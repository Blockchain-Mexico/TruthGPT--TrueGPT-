import tensorflow as tf
import tensorflow_model_optimization as tfmot
from transformers import GPT2Tokenizer, TFGPT2LMHeadModel
import horovod.tensorflow.keras as hvd
import tensorflow_datasets as tfds

# Initialize Horovod
hvd.init()

# Pin GPU to be used to process local rank (one GPU per process)
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], 'GPU')
    tf.config.experimental.set_memory_growth(gpus[hvd.local_rank()], True)

# Load GPT2 tokenizer and model
tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
model = TFGPT2LMHeadModel.from_pretrained('gpt2', return_dict=True)

# Define loss function and optimizer
loss_fn = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)
optimizer = tf.keras.optimizers.Adam()

# Define input pipeline
batch_size = 8 * hvd.size()  # Increase batch size to utilize multiple GPUs
train_dataset = tfds.load('wikipedia', split='train', shuffle_files=True)
train_dataset = train_dataset.map(lambda x: tokenizer(x['text'], return_tensors='tf')['input_ids'])
train_dataset = train_dataset.shard(hvd.size(), hvd.rank())
train_dataset = train_dataset.batch(batch_size)

# Define sparsity schedule
num_steps = 10000
end_step = num_steps - 1
pruning_schedule = tfmot.sparsity.keras.PolynomialDecay(initial_sparsity=0.0,
                                                         final_sparsity=0.5,
                                                         begin_step=0,
                                                         end_step=end_step)

# Define pruning parameters
pruning_params = {
    'pruning_schedule': pruning_schedule,
    'block_size': (1, 16),
    'block_pooling_type': 'AVG'
}

# Define pruning model
pruning_model = tfmot.sparsity.keras.prune_low_magnitude(model, **pruning_params)

# Define mixed precision policy
policy = tf.keras.mixed_precision.experimental.Policy('mixed_float16')
tf.keras.mixed_precision.experimental.set_policy(policy)

# Wrap optimizer with Horovod DistributedOptimizer
optimizer = hvd.DistributedOptimizer(optimizer)

# Compile pruning model
pruning_model.compile(optimizer=optimizer, loss=loss_fn, metrics=['accuracy'])

# Define pruning callbacks
pruning_callbacks = [
    tfmot.sparsity.keras.UpdatePruningStep(),
    tfmot.sparsity.keras.PruningSummaries(log_dir='logs/pruning'),
]

# Define early stopping callback
early_stopping_callback = tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=3)

# Train pruning model with Horovod
pruning_model.fit(train_dataset,
                  epochs=num_epochs,
                  steps_per_epoch=num_steps // hvd.size(),
                  callbacks=[pruning_callbacks, early_stopping_callback])

# Convert pruning model to regular model
model = tfmot.sparsity.keras.strip_pruning(pruning_model)
