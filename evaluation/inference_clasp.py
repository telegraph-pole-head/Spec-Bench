import argparse
import logging
import random
import time  # For timing checks

import numpy as np
import torch
from fastchat.utils import str_to_torch_dtype
from transformers import AutoTokenizer

# Assuming these are correctly adapted or available:
from evaluation.eval import reorg_answer_file, run_eval
from model.clasp.kv_cache import (
    clone_past_key_values,
    initialize_past_key_values,
)
from model.clasp.modeling_llama import (
    LlamaForCausalLM,  # Ensure this class is adapted as per previous steps
)

# Import CLASP utils
from model.clasp.utils import (  # Ensure this points to your utils
    CLaSp_Skip_Layer_Strategy_SeqParallel,  # The core DP algorithm
    # cosine_similarity,  # Helper for DP if needed outside
    # normalize_tensor,  # Helper for DP if needed outside
    prepare_logits_processor,
    sample,
    set_logger,
)

# Import pld
from model.pld.pld import find_candidate_pred_tokens


@torch.no_grad()
def clasp_forward(
    inputs,
    model,
    tokenizer,
    max_new_tokens,
    statistics=None,
    logits_processor=None,
    max_steps=512,
    args=None,
    # see_token=True,
    see_token=False,
):
    """
    CLaSp forward pass without tree-based speculative decoding.
    Uses autoregressive drafting with layer skipping and parallel verification.
    """
    global steps_since_last_optim, current_draft_skip_mask, first_acc_rates
    input_ids = inputs.input_ids.cuda()
    device = input_ids.device
    batch_size = input_ids.shape[0]
    assert batch_size == 1, "Only support batch size 1 for now!!"
    # --- Initialization ---
    input_ids_list = input_ids.tolist()
    generated_token_count = 0
    total_steps = 0
    accept_length_list = []
    L = model.config.num_hidden_layers
    M = int(L * args.skip_ratio)  # Number of layers to skip (M for CLaSp DP)
    optim_interval_steps = args.opt_interval
    K = args.draft_length_K
    DET = args.draft_exit_threshold
    HC = args.hc
    VC = args.vc
    # Initialize KV cache for the verify model
    # ! initialize_past_key_values now returns:
    # past_key_values (List[List[KVCache]]),
    # past_key_values_data (List[Tensor]),
    # current_length_data (Tensor)
    past_key_values, past_key_values_data, current_length_data = (
        initialize_past_key_values(model.model)
    )  # Pass base model
    model.past_key_values = past_key_values
    model.past_key_values_data = past_key_values_data
    model.current_length_data = current_length_data

    # --- Initial Prefill ---
    start_prefill = time.time()
    # The model's forward should internally use/update the KVCache objects
    # when use_cache=True. Pass the list of KVCache objects.

    prefill_outputs = model(
        input_ids=input_ids,
        past_key_values=past_key_values,  # Pass main cache list
        output_hidden_states=True,
    )

    prefill_token, _, _ = sample(prefill_outputs.logits, logits_processor)
    if see_token:
        logging.info(
            f"\nPrefill token: {prefill_token.item()}, str of token: {tokenizer.convert_ids_to_tokens(prefill_token.item())}"
        )
    input_ids_list[0].append(prefill_token.item())
    # next_draft_input_ids = prefill_token
    generated_token_count += 1

    logging.info(f"Prefill time: {time.time() - start_prefill:.4f}s")
    # After prefill, past_key_values_list and current_length_data should be updated automatically
    # by the KVCache.cat method called inside the model's forward pass.
    input_len = input_ids.shape[1]  # Current length is updated in current_length_data
    max_cache_len = getattr(model.config, "max_position_embeddings", 2048)
    if max_new_tokens > max_cache_len - input_len:
        logging.info(
            f"Warning: max_new_tokens ({max_new_tokens}) exceeds max cache length ({max_cache_len - input_len})."
        )
        max_new_tokens = max_cache_len - input_len - 1

    dp_statistics = {
        "accepted_len": [],
        "draft_len": [],
    }

    # Get hidden states from the *last input token* for the *first* DP calc
    last_token_hidden_states = None
    if prefill_outputs.hidden_states:
        last_token_hidden_states = [
            s[:, -1, :].squeeze(0) for s in prefill_outputs.hidden_states
        ]
    else:
        logging.info("No hidden states available from prefill.")
    # logging.info(
    #     f"Prefill hidden states length: {len(last_token_hidden_states)}, shape: {last_token_hidden_states[0].shape}"
    # )
    # hidden_size = last_token_hidden_states[0].shape[-1]
    # tokens_accepted_since_last_optim = 0

    # --- Generation Loop ---
    timings = {
        "total_step": [],
        "dp_optim": [],
        "draft_loop": [],
        "avg_draft_time": [],
        "verify": [],
        "accept_update": [],
        "misc_overhead": [],
    }
    step_end_time = time.time()  # Initialize before loop

    while generated_token_count < max_new_tokens and total_steps < max_steps:
        total_steps += 1
        start_step = time.time()
        timings["misc_overhead"].append(start_step - step_end_time)

        # --- 1. Layer Optimization (Run periodically) ---
        dp_start_time = time.time()
        run_dp = False
        if last_token_hidden_states is not None and (
            current_draft_skip_mask is None
            or steps_since_last_optim >= optim_interval_steps
        ):
            start_dp = time.time()
            logging.info(
                f"CLaSp DP: last statistics: avg accepted len: {np.mean(dp_statistics['accepted_len']) - 1}, "
                f"avg draft len: {np.mean(dp_statistics['draft_len']) - 1}, "
                f"avg accepted rate: {np.mean([0 if al==1 else 1 for al in dp_statistics['accepted_len']])}"
            )
            opt_past_key_values_data_list = [d.clone() for d in past_key_values_data]
            opt_current_length_data = (
                current_length_data.clone()
            )  # This tracks lengths for the opt cache
            # Create new KVCache objects pointing to the *cloned* data
            opt_kv_cache_list = clone_past_key_values(
                model, opt_past_key_values_data_list, opt_current_length_data
            )
            # logging.info(f"Running CLaSp DP Optimization at step {total_steps}...")
            current_draft_skip_mask = CLaSp_Skip_Layer_Strategy_SeqParallel(
                L=L,
                M=M,
                hidden_states_H=last_token_hidden_states,
                model_layers=model.model.layers,
                past_key_values=opt_kv_cache_list,
                device=device,
            )
            # logging.info("CLaSp DP Optimization: Layer to skip: ", current_draft_skip_mask)
            steps_since_last_optim = 0  # Reset counter
            logging.info(
                f"CLaSp DP time: {time.time() - start_dp:.4f}s. Skipped {torch.sum(current_draft_skip_mask)} layers."
            )
            logging.info(
                f"Drafting with mask: {current_draft_skip_mask.int().cpu().tolist()}"
            )
            dp_statistics = {
                "accepted_len": [],
                "draft_len": [],
            }
        dp_end_time = time.time()
        if run_dp:
            timings["dp_optim"].append(dp_end_time - dp_start_time)

        # --- 2. Drafting (Autoregressive) ---
        start_draft = time.time()
        draft_tokens = []
        # Use a *cloned* KV cache structure for drafting
        # We need to clone the underlying data tensors and create new KVCache objects
        draft_past_key_values_data_list = [d.clone() for d in past_key_values_data]
        draft_current_length_data = (
            current_length_data.clone()
        )  # This tracks lengths for the draft cache
        # Create new KVCache objects pointing to the *cloned* data
        draft_kv_cache_list = clone_past_key_values(
            model, draft_past_key_values_data_list, draft_current_length_data
        )

        first_draft_input_ids = torch.tensor(
            [input_ids_list[0][-1]], device=device
        ).unsqueeze(0)
        next_draft_input_ids = first_draft_input_ids

        continue_hc = True
        for draft_step_idx in range(K):
            # logging.info(f"token count: {len(draft_tokens) + generated_token_count}")
            # draft_i_start = time.time()
            # Pass the draft KVCache list to the model during drafting
            with model.self_draft(dynamic_skip_mask=current_draft_skip_mask):
                draft_outputs = model(  # Call the main model forward
                    input_ids=next_draft_input_ids,
                    past_key_values=draft_kv_cache_list,  # Use draft cache list
                    # past_key_values=past_key_values,
                    output_hidden_states=False,
                )
            # draft_i_end_time = time.time()
            # logging.info(
            #     f"Drafting foward step {draft_step_idx + 1} time: {draft_i_end_time - draft_i_start:.4f}s"
            # )
            draft_logits = draft_outputs.logits[:, -1, :]  # Logits for next token
            # draft_kv_cache_list is automatically updated by the forward pass
            # (DET check and sampling logic unchanged)
            if logits_processor:
                draft_logits_processed = logits_processor(
                    torch.tensor(input_ids_list, device=device), draft_logits
                )  # Pass full history?
            else:
                draft_logits_processed = draft_logits
            draft_probs = torch.softmax(draft_logits_processed, dim=-1)
            top1_prob = torch.max(draft_probs, dim=-1).values.item()
            if logits_processor:
                next_token = torch.multinomial(draft_probs, num_samples=1)
            else:
                next_token = torch.argmax(draft_logits_processed, dim=-1, keepdim=True)
            draft_tokens.append(next_token.item())
            next_draft_input_ids = next_token
            if top1_prob < DET and len(draft_tokens) > 0:
                break

            if next_token.item() == tokenizer.eos_token_id:
                continue_hc = False
                break

            if len(draft_tokens) + generated_token_count >= max_new_tokens - 2:
                continue_hc = False
                break
        # PLD HC
        if HC and continue_hc:
            all_ids = torch.tensor(
                [input_ids_list[0] + draft_tokens], device=device
            )
            max_new_draft = max_new_tokens - len(draft_tokens) - generated_token_count -2
            pld_candidate_tokens = find_candidate_pred_tokens(
                all_ids,
                max_ngram_size=3,
                num_pred_tokens=min(10, max_new_draft),
            )
            draft_tokens += pld_candidate_tokens.tolist()
        
        if see_token:
            logging.info(
                f"Drafted tokens: {draft_tokens}, str of tokens: {[tokenizer.convert_ids_to_tokens(t) for t in draft_tokens]}"
            )
        draft_loop_end_time = time.time()
        timings["draft_loop"].append(draft_loop_end_time - start_draft)
        num_drafted = len(draft_tokens)
        timings["avg_draft_time"].append(
            (draft_loop_end_time - start_draft) / num_drafted
            if num_drafted > 0
            else 0
        )
        # logging.info(
        #     f"Drafting time: {draft_loop_end_time - start_draft:.4f}s. Drafted {num_drafted} tokens."
        # )

        # --- 3. Verification (Parallel) ---
        start_verify = time.time()
        # verify_kv_cache_list = past_key_values_list  # Use main KVCache list
        current_seq_len = current_length_data[
            0
        ].item()  # Get current length BEFORE verify
        if current_seq_len != len(input_ids_list[0]) - 1:
            logging.info(
                f"Warning: Current length ({current_seq_len}) does not match input_ids_list length ({len(input_ids_list[0]) - 1})."
            )
        if num_drafted > 0:
            verify_input_list = [first_draft_input_ids.item()] + draft_tokens
            verify_input_ids = torch.tensor([verify_input_list], device=device)
        else:  # No tokens drafted, generate one with full model
            verify_input_ids = first_draft_input_ids
        # Run verification using the *full* model
        # Pass the main KVCache list
        verify_outputs = model(
            input_ids=verify_input_ids,
            past_key_values=past_key_values,  # Pass main cache list
            output_hidden_states=True,
            output_attentions=False,
        )
        verify_logits = verify_outputs.logits
        verify_hidden_states = verify_outputs.hidden_states
        verify_end_time = time.time()
        timings["verify"].append(verify_end_time - start_verify)
        # logging.info(f"Verification time: {verify_end_time - start_verify:.4f}s.")
        # NOTE: verify_kv_cache_list (past_key_values) is now updated by verify step

        # --- 4. Acceptance & Update ---
        start_accept = time.time()
        accepted_len = 0  # Number of *drafted* tokens accepted
        # rollback_len = num_drafted  # How many tokens in verify_kv_cache_list need potential rollback
        final_next_token = None
        last_accepted_token_hidden_states = None
        # Compare draft tokens with verify model predictions sequentially
        if num_drafted + 1 != verify_logits.shape[1]:
            logging.info(
                f"Warning: Number of drafted tokens + 1 :({num_drafted + 1}) does not match verify logits shape ({verify_logits.shape[1]})."
            )
        for k in range(num_drafted):
            verify_k = k - num_drafted - 1
            pred_logits_k = verify_logits[:, verify_k, :]
            if logits_processor:
                pred_logits_k = logits_processor(
                    torch.tensor(input_ids_list[0] + draft_tokens[:k], device=device),
                    pred_logits_k,
                )
            verify_probs_k = torch.softmax(pred_logits_k, dim=-1)
            if logits_processor:
                verify_token_k = torch.multinomial(verify_probs_k, num_samples=1).item()
            else:
                verify_token_k = torch.argmax(pred_logits_k, dim=-1).item()
            if draft_tokens[k] == verify_token_k:
                # # Accept draft token
                accepted_len += 1
                continue
            else:
                # Mismatch: Need to rollback KV cache and accept verify_token_k
                final_next_token = verify_token_k
                # accepted_len = k
                if verify_hidden_states:
                    try:
                        last_accepted_token_hidden_states = [
                            s[:, verify_k, :].squeeze(0) for s in verify_hidden_states
                        ]
                    except IndexError:
                        last_accepted_token_hidden_states = None
                else:
                    last_accepted_token_hidden_states = None
                break  # Stop acceptance loop
        # If all draft tokens accepted, sample the bonus token
        if accepted_len == num_drafted:
            # rollback_len = num_drafted  # No rollback needed, but length is num_drafted
            final_logits = verify_logits[:, -1, :]  # Logits after last draft token
            if logits_processor:
                final_logits = logits_processor(
                    torch.tensor(input_ids_list + [draft_tokens[-1]], device=device),
                    final_logits,
                )
            final_probs = torch.softmax(final_logits, dim=-1)
            if logits_processor:
                final_next_token = torch.multinomial(final_probs, num_samples=1).item()
            else:
                final_next_token = torch.argmax(final_logits, dim=-1).item()
            # Hidden states correspond to the bonus token prediction point (index num_drafted)
            if verify_hidden_states:
                try:
                    last_accepted_token_hidden_states = [
                        s[:, -1, :].squeeze(0) for s in verify_hidden_states
                    ]
                except IndexError:
                    last_accepted_token_hidden_states = None
            else:
                last_accepted_token_hidden_states = None
        if num_drafted > 0:
            first_acc_rates.append(1 if accepted_len > 0 else 0)

        # --- Update Sequence and KV Cache Length ---
        # Add accepted draft tokens to sequence
        input_ids_list[0].extend(draft_tokens[:accepted_len])
        # Add the final token (either mismatch or bonus)
        input_ids_list[0].append(final_next_token)
        if see_token:
            logging.info(
                f"Accepted tokens: {draft_tokens[:accepted_len]}, str of tokens: {[tokenizer.convert_ids_to_tokens(t) for t in draft_tokens[:accepted_len]]}"
            )
            logging.info(
                f"Final (bonus) token: {final_next_token}, str of token: {tokenizer.convert_ids_to_tokens(final_next_token)}"
            )
        # Update the current_length_data tensor to reflect the true length
        new_len = current_seq_len + accepted_len + 1
        current_length_data.fill_(new_len)  # Set length for ALL layers
        # Total accepted tokens in this verify step
        step_accept_len = accepted_len + 1
        accept_length_list.append(step_accept_len)
        generated_token_count += step_accept_len
        steps_since_last_optim += 1
        dp_statistics["accepted_len"].append(step_accept_len)
        dp_statistics["draft_len"].append(num_drafted)
        if last_accepted_token_hidden_states is None:
            logging.info(
                "Warning: No hidden states available for the last accepted token."
            )
            if verify_hidden_states is None:
                logging.info(
                    "Warning: No hidden states available from the verify model."
                )

        last_token_hidden_states = last_accepted_token_hidden_states
        accept_update_end_time = time.time()
        timings["accept_update"].append(accept_update_end_time - start_accept)


        step_end_time = time.time()
        timings["total_step"].append(step_end_time - start_step)
        # --- Check for EOS ---
        if tokenizer.eos_token_id in input_ids_list[0][-step_accept_len:]:
            # logging.info(
            #     f"EOS token found in generated sequence. Stopping generation at step {total_steps}."
            # )
            break

    # --- Final Output ---
    # (Output logic unchanged)
    output_ids = torch.tensor([input_ids_list[0][:]], device=device)  # Generated part
    avg_accept_len = np.mean(accept_length_list) if accept_length_list else 0
    # logging.info(
    #     f"Finished. Total steps: {total_steps}, Total generated: {generated_token_count}, Avg accept/step: {avg_accept_len:.2f}"
    # )

    # --- Print Timings ---
    # logging.info(f"\ntotal_steps: {total_steps}")
    logging.info("--- Performance Timings (Average per Step) ---")
    for key, values in timings.items():
        if values:
            avg_time = np.mean(values)
            logging.info(f"{key}: {avg_time:.4f}s")
        else:
            logging.info(f"{key}: N/A (not run or no steps)")
    logging.info(f"Average Acceptance Length: {(avg_accept_len - 1):.2f}")
    logging.info(
        f"Finished. Total steps: {total_steps}, Total generated: {generated_token_count}"
    )

    return output_ids, generated_token_count, total_steps, accept_length_list


def seed_everything(seed=64):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


# --- Main Execution Block ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-path",
        type=str,
        required=True,
    )
    parser.add_argument("--model-id", type=str, required=True)
    parser.add_argument("--answer-file", type=str, help="The output answer file.")
    parser.add_argument(
        "--num-choices",
        type=int,
        default=1,
        help="How many completion choices to generate.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=1024,
        help="The maximum number of new generated tokens.",
    )
    parser.add_argument(
        "--num-gpus-per-model",
        type=int,
        default=1,
        help="The number of GPUs per model.",
    )
    parser.add_argument(
        "--num-gpus-total", type=int, default=1, help="The total number of GPUs."
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="The temperature for clasp sampling.",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=0.85,
        help="The top-p for sampling.",
    )
    parser.add_argument(
        "--dtype",
        type=str,
        default="float16",
        choices=["float32", "float64", "float16", "bfloat16"],
        help="Override the default dtype. If not set, it will use float16 on GPU.",
    )
    parser.add_argument(
        "--bench-name",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=2024,
        help="The sampling seed.",
    )
    parser.add_argument(
        "--question-begin",
        type=int,
        help="A debug option. The begin index of questions.",
    )
    parser.add_argument(
        "--question-end", type=int, help="A debug option. The end index of questions."
    )

    # --- Add/Modify args for CLaSp ---
    parser.add_argument(
        "--skip-ratio",
        type=float,
        default=0.5,  # Example value, needs tuning
        # required=True,  # M is a key param for CLaSp DP
        help="The target number of layers to skip (M for CLaSp DP).",
    )
    parser.add_argument(
        "--opt-interval",
        type=int,
        default=1,  # Default: Optimize before every draft step
        help="The interval (in terms of accepted tokens) between CLaSp DP optimizations (Section 3.7).",
    )
    parser.add_argument(
        "--draft-exit-threshold",
        type=float,
        default=0.7,  # Example value, needs tuning
        help="Draft-Exiting Threshold (DET) based on draft model confidence (Section 5.3.3).",
    )
    parser.add_argument(
        "--draft-length-K",
        type=int,
        default=8,
        help="Maximum number of tokens to draft in each step (K).",
    )
    parser.add_argument(
        "-hc",
        action="store_true",
        default=False,
        help="Use the Hrizontal Cascase with PLD for CLaSp.",
    )
    parser.add_argument(
        "-vc",
        action="store_true",
        default=False,
        help="Use the Vertical Cascade with PLD for CLaSp.",
    )

    args = parser.parse_args()

    args.model_name = (
        args.model_id
        + "-clasp-"
        + str(args.dtype)
        + "-temp-"
        + str(args.temperature)
        + "-topp-"
        + str(args.top_p)
        + "-seed-"
        + str(args.seed)
        + "-maxntok-"
        + str(args.max_new_tokens)
        + f"-I{args.opt_interval}"
        + f"-K{args.draft_length_K}"
        + f"-DET{args.draft_exit_threshold}"
        + f"-skip{args.skip_ratio}"
        + "-hc" * args.hc
        + "-vc" * args.vc
    )  # Include CLaSp params
    answer_file = f"data/{args.bench_name}/model_answer/{args.model_name}.jsonl"
    set_logger()  # Assuming set_logger is in clasp_utils or imported

    print(f"Output to {answer_file}")
    question_file = f"data/{args.bench_name}/question.jsonl"
    if args.answer_file:
        answer_file = args.answer_file

    # --- Model Loading ---
    print(f"Loading model: {args.model_path}")
    # Crucially, ensure output_hidden_states can be enabled if not default
    # config_kwargs = {"output_hidden_states": True} # Or modify config before loading
    model = LlamaForCausalLM.from_pretrained(
        args.model_path,
        torch_dtype=str_to_torch_dtype(args.dtype),
        low_cpu_mem_usage=True,
        device_map="auto",
        # config=AutoConfig.from_pretrained(args.model_path, **config_kwargs) # If needed
    )
    model.eval()  # Ensure dropout etc. are off

    # Explicitly enable in config if needed AFTER loading
    model.config.output_hidden_states = True
    # Also for the base model if structure is nested e.g. model.model
    if hasattr(model, "model"):
        model.model.config.output_hidden_states = True

    tokenizer = AutoTokenizer.from_pretrained(args.model_path)
    print("Model and tokenizer loaded.")

    # --- Logits Processor ---
    if args.temperature > 1e-5:
        logits_processor = prepare_logits_processor(
            temperature=args.temperature, top_p=args.top_p
        )
    else:
        logits_processor = None

    # --- CLaSp specific setup ---
    # No fixed layer set needed initially, it's dynamic.
    seed_everything(args.seed)  # Assuming seed_everything is available

    # No Bayes optimizer setup needed for CLaSp core logic

    # Statistics dict can be simplified or removed if not used by run_eval
    statistics = {
        "accept_length_list": [],  # Maybe track this
        # Add CLaSp specific args if needed by forward func or eval
        "skip_ratio": args.skip_ratio,
        "opt_interval": args.opt_interval,
        "draft_exit_threshold": args.draft_exit_threshold,
    }

    current_draft_skip_mask = None
    steps_since_last_optim = 0
    first_acc_rates = []

    # --- Run Evaluation ---
    run_eval(
        model=model,
        tokenizer=tokenizer,
        forward_func=clasp_forward,  # Use the CLaSp forward function
        model_id=args.model_id,
        question_file=question_file,
        question_begin=args.question_begin,
        question_end=args.question_end,
        answer_file=answer_file,
        max_new_tokens=args.max_new_tokens,
        num_choices=args.num_choices,
        num_gpus_per_model=args.num_gpus_per_model,
        num_gpus_total=args.num_gpus_total,
        statistics=statistics,  # Pass simplified stats if needed
        logits_processor=logits_processor,
        args=args,  # Pass full args object to forward function
    )

    print("First acceptance rates:", np.mean(first_acc_rates))

    reorg_answer_file(answer_file)
    print("Evaluation finished.")
