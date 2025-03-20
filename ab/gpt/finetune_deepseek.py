from transformers import (
    Trainer, TrainingArguments, AutoTokenizer, AutoModelForCausalLM,
    BitsAndBytesConfig, DataCollatorForSeq2Seq, EarlyStoppingCallback
)
from peft import (
    get_peft_model, LoraConfig, PeftModel,
    prepare_model_for_kbit_training, set_peft_model_state_dict
)
from datasets import load_dataset, load_from_disk
import torch
import json
import os
import sys
import random
import warnings
from dataset_preparation import DatasetPreparation

os.environ["WANDB_MODE"] = "disabled"
device = torch.device("cuda" if torch.cuda.is_available()  else "cpu")


def create_prompt(data_point):
    """
    Creates a prompt for the LLM
    """
    return f"""
        ### Input:
        {data_point["question"]}

        ### Response:
        {data_point["answer"]}
    """

def tokenize(prompt, tokenizer):
    """
    Tokenizes a string
    """
    return tokenizer(
        prompt,
        truncation=True,
        max_length=tokenizer.model_max_length,
        padding=False,
        return_tensors=None,
    )

def main(tuned_model_version, dataset_path):
    """
    The main function for loading data, setting up the model and fine-tuning
    """

    # Write training output to the file
    # log_filename = f"training_logs_{tuned_model_version}.txt"
    # sys.stdout = open(log_filename, "w")

    hf_directories = {
        1: "deepseek-ai/DeepSeek-Coder-V2-Lite-Base",
        2: "deepseek-ai/deepseek-coder-1.3b-base",
        3: "deepseek-ai/deepseek-coder-1.3b-base",
        3.5: "deepseek-ai/deepseek-coder-1.3b-base",
        4: "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        5: "deepseek-ai/deepseek-coder-7b-base-v1.5",
        6: "deepseek-ai/deepseek-math-7b-base",
        7: "deepseek-ai/deepseek-coder-7b-instruct-v1.5"
    }

    hf_directory = hf_directories.get(tuned_model_version)
    if hf_directory is None:
        raise ValueError(f"Unknown model version: {tuned_model_version}")
    print(f"Using model: {hf_directory}")

    tokenizer = AutoTokenizer.from_pretrained(hf_directory)
    tokenizer.add_eos_token = True
    tokenizer.pad_token_id = 0
    tokenizer.padding_side = "right"

    # quantization_config_4bit = BitsAndBytesConfig(
    #     load_in_4bit=True,
    #     bnb_4bit_compute_dtype = torch.float16
    # )

    quantization_config_8bit = BitsAndBytesConfig(
        load_in_8bit=True,
        bnb_8bit_compute_dtype=torch.float16
    )

    # quantization_config_16bit = BitsAndBytesConfig(
    #     load_in_16bit=True
    # )

    model = AutoModelForCausalLM.from_pretrained(hf_directory,
                                                 trust_remote_code=True,
                                                 device_map="auto",
                                                 quantization_config=quantization_config_8bit)

    # trying to load mapped datasets
    try:
        tokenized_train_dataset = load_from_disk(
            f"Finetuned_models/tuned_model_v{tuned_model_version}/tokenized_train_dataset")
        tokenized_val_dataset = load_from_disk(
            f"Finetuned_models/tuned_model_v{tuned_model_version}/tokenized_val_dataset")
        print("Datasets loaded successfully.")
    except Exception as e:
        print(f"Dataset loading failed: {e}")
        dataset = load_dataset('json', data_files=dataset_path)
        shuffled_dataset = dataset['train'].shuffle(seed=42)

        train_dataset = shuffled_dataset.train_test_split(test_size=0.2)["train"]
        eval_dataset = shuffled_dataset.train_test_split(test_size=0.2)["test"]

        num_proc = os.cpu_count() // 2
        tokenized_train_dataset = train_dataset.map(lambda data_point: tokenize(create_prompt(data_point), tokenizer),
                                                    num_proc=num_proc)
        tokenized_val_dataset = eval_dataset.map(lambda data_point: tokenize(create_prompt(data_point), tokenizer),
                                                 num_proc=num_proc)

        tokenized_train_dataset.save_to_disk(f"Finetuned_models/tuned_model_v{tuned_model_version}/"
                                             f"tokenized_train_dataset")
        tokenized_val_dataset.save_to_disk(f"Finetuned_models/tuned_model_v{tuned_model_version}/"
                                           f"tokenized_val_dataset")
        print("Datasets have been processed and saved.")

    # put model back into training mode
    model.train()
    model = prepare_model_for_kbit_training(model)

    # LoRA config
    peft_config = LoraConfig(
        r=32,
        lora_alpha=32,
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
        ],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )

    # Applying LoRA to the model
    model = get_peft_model(model, peft_config)

    # Training Arguments
    training_args = TrainingArguments(
        num_train_epochs=35,
        warmup_steps=100,
        optim="adamw_torch",
        learning_rate=1e-5,
        logging_steps=10,
        max_grad_norm=1.0,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=8,
        lr_scheduler_type="cosine",
        gradient_checkpointing=False,
        fp16=True,
        eval_strategy="epoch",
        save_strategy="epoch",
        output_dir=f"./Finetuned_models/tuned_model_v{tuned_model_version}/output",
        logging_dir=f"./Finetuned_models/tuned_model_v{tuned_model_version}/logs",
        weight_decay=0.01,
        save_total_limit=3,
        load_best_model_at_end=True
    )

    # trainer initialization
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_train_dataset,
        eval_dataset=tokenized_val_dataset,
        data_collator=DataCollatorForSeq2Seq(
        tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding=True
        ),
        callbacks=[EarlyStoppingCallback(early_stopping_patience=2)]
    )

    print(f"Using device: {device}")

    # Variant I: Fine-tuning of the model from the start
    trainer.train()

    # # Variant II: Resume fine-tuning of the model from the checkpoint
    # resume_from_checkpoint = f"Finetuned_models/tuned_model_v{tuned_model_version}/output/checkpoint-????"
    # if resume_from_checkpoint:
    #     if os.path.exists(resume_from_checkpoint):
    #         print(f"Resuming training from {resume_from_checkpoint}")
    #     else:
    #         print("Checkpoint not found, starting training from scratch")
    #         resume_from_checkpoint = None
    # trainer.train(resume_from_checkpoint=resume_from_checkpoint)

    eval_results = trainer.evaluate()
    print("Evaluation results:", eval_results)

    model.save_pretrained(f"./Finetuned_models/tuned_model_v{tuned_model_version}/model")
    tokenizer.save_pretrained(f"./Finetuned_models/tuned_model_v{tuned_model_version}/tokenizer")

    # sys.stdout.close()
    # sys.stdout = sys.__stdout__
    # print(f"\nTraining log saved to {log_filename}")


def generating_response_cycle_model_finetuned(tuned_model_version, input_file_path, output_file_path, logs_file):
    hf_directories = {
        1: "deepseek-ai/DeepSeek-Coder-V2-Lite-Base",
        2: "deepseek-ai/deepseek-coder-1.3b-base",
        3: "deepseek-ai/deepseek-coder-1.3b-base",
        3.5: "deepseek-ai/deepseek-coder-1.3b-base",
        4: "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        5: "deepseek-ai/deepseek-coder-7b-base-v1.5",
        6: "deepseek-ai/deepseek-math-7b-base",
        7: "deepseek-ai/deepseek-coder-7b-instruct-v1.5"
    }

    hf_directory = hf_directories.get(tuned_model_version)
    if hf_directory is None:
        raise ValueError(f"Unknown model version: {tuned_model_version}")
    print(f"Using model: {hf_directory}")

    quantization_config_8bit = BitsAndBytesConfig(
        load_in_8bit=True
    )

    model = AutoModelForCausalLM.from_pretrained(hf_directory,
                                                 trust_remote_code=True,
                                                 device_map="auto",
                                                 quantization_config=quantization_config_8bit)
    tokenizer = AutoTokenizer.from_pretrained(hf_directory)
    output_dir = f"./Finetuned_models/tuned_model_v{tuned_model_version}/model"
    model = PeftModel.from_pretrained(model, output_dir)

    with open(input_file_path, "r") as f:
        data = json.load(f)

    i = 0
    data_len = len(data)
    random.shuffle(data)
    processed_data = []

    with open(logs_file, "w") as output_file:
        for i, entry in enumerate(data):
            hyperparameters = entry['prm']
            prm_names = ", ".join(hyperparameters.keys())

            eval_prompt = f"""
            ### Input:
            Generate only the values (don't provide any explanation) of the hyperparameters ({prm_names}) of a 
            given model: {entry['metric']} for the task: {entry['task']} on dataset: {entry['dataset']}, 
            with transformation: {entry['transform_code']}, so that the model achieves accuracy = {entry['accuracy']} 
            with number of training epochs = {entry['epoch']}. 
            Code of that model:\n {entry['nn_code']}

            ### Response:
            """

            model_input = tokenizer(eval_prompt, return_tensors="pt").to("cuda")

            with torch.no_grad():
                output = model.generate(**model_input, max_new_tokens=150, pad_token_id=tokenizer.pad_token_id)
                response_text = tokenizer.decode(output[0], skip_special_tokens=True)

            response_text = response_text.split("### Response:")[-1].strip()

            # Save Logs
            output_file.write(f"Model #{i + 1}\n")
            output_file.write(f"Prompt:\n{eval_prompt}\n")
            output_file.write(f"Response:\n{response_text}\n\n")

            # Save Model's Response to JSON
            entry['Response'] = response_text
            processed_data.append(entry)

            print(f"Got {i + 1} responses out of {len(data)}")

    print(f"All responses are saved in {logs_file}")

    with open(output_file_path, "w") as f:
        json.dump(processed_data, f, indent=4)

    print(f"All hyperparameters have been successfully saved to {output_file_path}")




if __name__ == "__main__":
    tuned_model_version = 1

    dataset_raw = f"Dataset/LEMUR_raw_2.json"
    dataset_prepared_prompt = f"Dataset/LEMUR_prepared_2.json"


    # ---------- 1. DATASET PREPARATION STAGE ----------
    dataset_prep = DatasetPreparation()
    # all_data = dataset_prep.collect_all_data()
    # dataset_prep.save_as_json(all_data, 'Dataset/Dataset_FT_2.json')

        # Create a raw LEMUR dataset
    # dataset_prep.test_api(dataset_raw)
        # Convert a LEMUR raw dataset to a prompt
    # dataset_prep.update_json_with_nn_code(dataset_raw, f"Dataset/LEMUR_raw_2_500nn.json")

    # dataset_prep.create_new_dataset_for_training_once(
    #     "Dataset/FINE_all_data_RAW_for_evaluation_Max_Accuracy_prepared.json",
    #     "Dataset/FINE_all_data_RAW_for_evaluation_Max_Accuracy_prepared.json")

    # DatasetPreparation.count_code_lines()


    # ---------- 3. DEEPSEEK-CODER FINE-TUNING STAGE ----------
    main(tuned_model_version, dataset_prepared_prompt)

    # ---------- 4. DEEPSEEK-CODER TESTING & RECEIVING RESPONSES STAGE ----------
    # Dataset 500 Models
    # dataset_raw_500 = f"Dataset/LEMUR_raw_2_500.json"

    # Base Model Paths
    # output_file_path = "Dataset/ds_responses_1coder-v2-base-lite_500.json"
    # logs_file = f"Logs/logs_responses_1coder-v2-base-lite_500.txt"

    # Fine-tuned Model Paths
    # output_file_path = "Dataset/ds_responses_1ft_500.json"
    # logs_file = f"Logs/logs_responses_1ft_500.txt"

    # generating_response_cycle_model_finetuned(tuned_model_version, dataset_raw_500, output_file_path, logs_file)