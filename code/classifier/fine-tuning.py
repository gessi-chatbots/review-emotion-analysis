import argparse
import pandas as pd
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification, Trainer, TrainingArguments
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
import os
import logging
import numpy as np

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Define label maps
MULTICLASS_LABEL_MAP = {
    'Joy': 0, 'Sadness': 1, 'Anger': 2, 'Fear': 3, 'Trust': 4, 'Disgust': 5,
    'Surprise': 6, 'Anticipation': 7, 'Neutral': 8
}

BINARY_LABEL_MAP = {'Positive': 1, 'Negative': 0}

def parse_args():
    parser = argparse.ArgumentParser(description="Fine-tune Hugging Face models for emotion classification.")
    parser.add_argument("--model-id", required=True, help="Hugging Face model ID")
    parser.add_argument("--tokenizer-id", required=True, help="Hugging Face tokenizer ID")
    parser.add_argument("--input-csv", required=True, help="Path to the input CSV file")
    parser.add_argument('--multiclass', action='store_true', help='Set this flag to use multiclass classification')
    return parser.parse_args()

def load_data(input_csv, multiclass):
    logger.info("Loading data from %s", input_csv)
    df = pd.read_csv(input_csv)
    logger.info("Data loaded. Number of rows: %d", len(df))
    logger.info("Dataset head:\n%s", df.head())
    
    if multiclass:
        df['labels'] = df[['emotion-A', 'emotion-B']].apply(
            lambda x: [MULTICLASS_LABEL_MAP[x[0]], MULTICLASS_LABEL_MAP[x[1]]] if pd.notna(x[1]) else [MULTICLASS_LABEL_MAP[x[0]]],
            axis=1
        )
        df['labels'] = df['labels'].apply(lambda x: x[0])  # Flatten for stratification
    else:
        binary_labels = {emotion: [] for emotion in MULTICLASS_LABEL_MAP.keys()}
        for _, row in df.iterrows():
            for emotion in MULTICLASS_LABEL_MAP.keys():
                binary_labels[emotion].append(
                    BINARY_LABEL_MAP['Positive'] if emotion in [row['emotion-A'], row['emotion-B']] else BINARY_LABEL_MAP['Negative']
                )
        logger.info("Binary labels prepared for emotions.")
        return df, binary_labels
    return df

def display_emotion_counts(df):
    # Count occurrences of each emotion in emotion-A and emotion-B columns
    emotion_counts = df[['emotion-A', 'emotion-B']].stack().value_counts()
    
    # Display counts
    logger.info("Number of instances per emotion:")
    for emotion, count in emotion_counts.items():
        logger.info(f"{emotion}: {count}")

def preprocess_data(texts, labels, tokenizer):
    logger.info("Preprocessing data with tokenizer %s", tokenizer.name_or_path)
    encodings = tokenizer(texts, truncation=True, padding=True, max_length=128)
    return encodings, labels

def compute_metrics(predictions):
    preds, labels = predictions
    preds = preds.argmax(axis=1)  # Get predicted class indices
    accuracy = accuracy_score(labels, preds)
    precision, recall, f1, _ = precision_recall_fscore_support(labels, preds, average=None)

    # Convert NumPy arrays to Python lists for JSON serialization
    return {
        "accuracy": float(accuracy),
        "precision": precision.tolist(),
        "recall": recall.tolist(),
        "f1": f1.tolist(),
    }

def cross_validate_model(model_id, tokenizer_id, texts, labels, k=10, multiclass=True, output_dir="./output"):
    logger.info("Starting cross-validation with k=%d", k)
    skf = StratifiedKFold(n_splits=k, shuffle=True, random_state=42)
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_id)

    fold_metrics = []
    fold_detailed_metrics = []

    for fold, (train_idx, test_idx) in enumerate(skf.split(texts, labels)):
        logger.info("Processing fold %d/%d", fold + 1, k)
        logger.info("Training instances %d", len(train_idx))
        logger.info("Testing instances %d", len(test_idx))
        
        # Split data
        train_texts, test_texts = texts[train_idx], texts[test_idx]
        train_labels, test_labels = labels[train_idx], labels[test_idx]
        
        # Preprocess
        train_encodings, train_labels = preprocess_data(train_texts.tolist(), train_labels.tolist(), tokenizer)
        test_encodings, test_labels = preprocess_data(test_texts.tolist(), test_labels.tolist(), tokenizer)
        
        # Replace TensorDataset with a list of dictionaries
        train_dataset = [
            {key: torch.tensor(val[i]) for key, val in train_encodings.items()} | {"labels": torch.tensor(train_labels[i])}
            for i in range(len(train_labels))
        ]
        test_dataset = [
            {key: torch.tensor(val[i]) for key, val in test_encodings.items()} | {"labels": torch.tensor(test_labels[i])}
            for i in range(len(test_labels))
        ]

        
        # Load model
        model = AutoModelForSequenceClassification.from_pretrained(
            model_id, num_labels=len(MULTICLASS_LABEL_MAP) if multiclass else 2
        )
        
        # Define training arguments
        training_args = TrainingArguments(
            output_dir=os.path.join(output_dir, f"fold-{fold}"),
            num_train_epochs=3,
            per_device_train_batch_size=16,
            eval_strategy="epoch",
            save_strategy="epoch",
            logging_dir=os.path.join(output_dir, f"fold-{fold}/logs"),
            report_to="none"
        )
        
        # Define Trainer
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset,
            eval_dataset=test_dataset,
            compute_metrics=compute_metrics
        )
        
        # Train and evaluate
        trainer.train()
        eval_metrics = trainer.evaluate()
        logger.info("Fold %d metrics: %s", fold + 1, eval_metrics)

        # Save fold-specific metrics
        fold_metrics.append(eval_metrics)
        fold_detailed_metrics.append({"fold": fold + 1, **eval_metrics})
    
    # Save detailed metrics for all folds
    fold_metrics_df = pd.DataFrame(fold_detailed_metrics)
    fold_metrics_csv = os.path.join(output_dir, "fold_metrics.csv")
    fold_metrics_df.to_csv(fold_metrics_csv, index=False)
    logger.info("Detailed metrics for all folds saved to %s", fold_metrics_csv)

    # Aggregate metrics
    avg_metrics = aggregate_metrics(fold_metrics)
    logger.info("Average metrics across folds: %s", avg_metrics)
    return avg_metrics

def aggregate_metrics(fold_metrics):
    avg_metrics = {
        "accuracy": np.mean([metrics["eval_accuracy"] for metrics in fold_metrics]),
        "precision": np.mean([np.mean(metrics["eval_precision"]) for metrics in fold_metrics]),
        "recall": np.mean([np.mean(metrics["eval_recall"]) for metrics in fold_metrics]),
        "f1": np.mean([np.mean(metrics["eval_f1"]) for metrics in fold_metrics])
    }
    return avg_metrics

def save_metrics(metrics, output_csv):
    logger.info("Saving metrics to %s", output_csv)
    metrics_df = pd.DataFrame(metrics, index=[0])
    metrics_df.to_csv(output_csv, index=False)
    logger.info("Metrics saved successfully.")

def main():
    args = parse_args()
    logger.info("Arguments received: %s", args)
    
    df = load_data(args.input_csv, args.multiclass)
    display_emotion_counts(df)
    texts = df['sentence'].to_numpy()
    labels = df['labels'].to_numpy()

    output_dir = "./evaluation"
    os.makedirs(output_dir, exist_ok=True)

    avg_metrics = cross_validate_model(
        model_id=args.model_id,
        tokenizer_id=args.tokenizer_id,
        texts=texts,
        labels=labels,
        k=10,
        multiclass=args.multiclass,
        output_dir=output_dir
    )
    
    avg_metrics_csv = os.path.join(output_dir, f"{args.model_id}-{'multiclass' if args.multiclass else 'binary'}_metrics.csv")
    save_metrics(avg_metrics, avg_metrics_csv)

if __name__ == "__main__":
    main()
