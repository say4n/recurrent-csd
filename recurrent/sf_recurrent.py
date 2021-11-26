import os
import time

import numpy as np
import strawberryfields as sf
import tensorflow as tf
from loguru import logger

# Hides info messages from TensorFlow.
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

# Number of layers of the Dolinar receiver. Selecting 4 as the most basic,
# non-trivial case.
NUM_LAYERS = 4

# Number of quantum modes. Basic 2-mode case.
NUM_MODES = 2

# Number of variables being optimized per mode.
NUM_VARIABLES = 1

# Signal amplitude. Default is 1.0.
SIGNAL_AMPLITUDE = 1.0

# Fock backend.
ENGINE = sf.Engine("tf", backend_options={"cutoff_dim": 6})

# Number of iterations to train for.
NUM_ITERATIONS = 1000


def generate_nth_layer(layer_number, engine):
    """Generates the nth layer of the Dolinar receiver.
    Given the `layer_number` and `engine` as input, it returns a
    function that generates the necessary quantum circuit for the n-th layer of
    the Dolinar receiver.
    """
    # Reset engine if a program has been executed.
    if engine.run_progs:
        engine.reset()

    # Need k values for the splits of the coherent state.
    amplitudes =  np.ones(NUM_LAYERS) * (SIGNAL_AMPLITUDE / NUM_LAYERS)

    def quantum_layer(input_codeword, displacement_magnitudes_for_each_mode):
        logger.debug(f"{input_codeword =}")
        logger.debug(f"{displacement_magnitudes_for_each_mode =}")

        program = sf.Program(NUM_MODES)

        mapping = {}
        params = {}

        for nth_mode in range(NUM_MODES):
            params[f"input_codeword_arg_{nth_mode}"] =  program.params(f"input_codeword_arg_{nth_mode}")
            mapping[f"input_codeword_arg_{nth_mode}"] = input_codeword[nth_mode]

            params[f"displacement_magnitudes_for_each_mode_arg_{nth_mode}"] =  program.params(f"displacement_magnitudes_for_each_mode_arg_{nth_mode}")
            mapping[f"displacement_magnitudes_for_each_mode_arg_{nth_mode}"] = displacement_magnitudes_for_each_mode[nth_mode]

        with program.context as q:
            # Prepare the coherent states for the layer. Appropriately scales
            # the amplitudes for each of the layers.
            for m in range(NUM_MODES):
                sf.ops.Coherent(amplitudes[layer_number] * params[f"input_codeword_arg_{m}"]) | q[m]

            # Displace each of the modes by using the displacement magnitudes
            # generated by the ML backend.
            for m in range(NUM_MODES):
                sf.ops.Dgate(params[f"displacement_magnitudes_for_each_mode_arg_{m}"]) | q[m]

            # Perform measurements.
            sf.ops.MeasureFock() | q

        return engine.run(program, args=mapping)

    return quantum_layer


def build_model(name="predictor"):
    """
    Builds a tensorflow model layer by layer utilising the sequential API.
    """
    model = tf.keras.Sequential([
        tf.keras.Input(shape=(NUM_MODES * NUM_VARIABLES + NUM_LAYERS, ), name="input-layer"),
        tf.keras.layers.Dense(8, activation="relu", name="layer-1"),
        tf.keras.layers.Dense(8, activation="relu", name="layer-2"),
        tf.keras.layers.Dense(NUM_MODES * NUM_VARIABLES, activation="sigmoid", name="output-layer")
    ], name=name)

    return model


def generate_random_codeword():
    """
    Generates a random codeword for `NUM_MODES` modes.
    """
    return tf.Variable(np.random.choice([-1, +1], size=NUM_MODES), dtype=tf.float32)


@tf.function
def loss_metric(prediction, target, true_tensor, false_tensor):
    """
    Computes the numerical loss incurred on generating `prediction` instead of
    `target`.
    Both `prediction` and `target` are tensors.
    """
    indices_where_input_codeword_was_minus = tf.where(target == -1, true_tensor, false_tensor)
    indices_where_measurement_is_not_positive = tf.where(prediction <= 0, true_tensor, false_tensor)

    combined_indices = tf.logical_and(
        indices_where_input_codeword_was_minus,
        indices_where_measurement_is_not_positive
    )

    return tf.reduce_sum(tf.cast(combined_indices, tf.float32))


def step():
    """
    Runs a single step of optimization for a single value of alpha across all
    layers of the Dolinar receiver.
    """
    global model, layers, optimizer

    previous_predictions = tf.random.normal([NUM_MODES * NUM_VARIABLES])

    input_codeword = generate_random_codeword()

    with tf.GradientTape() as tape:
        loss = tf.Variable(0.0)

        for nth_layer in range(NUM_LAYERS):
            one_hot_layer_vector = tf.one_hot(nth_layer, NUM_LAYERS)

            input_vector = tf.concat([previous_predictions, one_hot_layer_vector], 0)
            input_vector = tf.expand_dims(input_vector, 0)

            logger.debug(f"{input_vector =}")

            predicted_displacements = model(input_vector)
            squeezed_predicted_displacements = tf.squeeze(predicted_displacements)

            measurement_of_nth_layer = layers[nth_layer](
                input_codeword,
                2 * SIGNAL_AMPLITUDE * squeezed_predicted_displacements)

            logger.debug(f"{measurement_of_nth_layer.samples = }")

            true_tensor = tf.fill((NUM_MODES), True)
            false_tensor = tf.fill((NUM_MODES), False)

            loss.assign_add(
                loss_metric(
                    measurement_of_nth_layer.samples,
                    input_codeword,
                    true_tensor,
                    false_tensor
                ) / NUM_MODES
            )

            previous_predictions = squeezed_predicted_displacements

        logger.debug(f"Accumulated loss: {loss}")

    breakpoint()
    gradients = tape.gradient(loss, model.trainable_variables)
    optimizer.apply_gradients(zip(gradients, model.trainable_variables))


if __name__ == '__main__':
    # ML model to predict the displacement magnitude for each of the layers of
    # the Dolinar receiver.
    logger.info("Building model.")
    model = build_model(f"predictor-l-{NUM_LAYERS}-alpha-{SIGNAL_AMPLITUDE}-modes-{NUM_MODES}")
    logger.info("Done.")

    # Layers of the Dolinar receiver.
    logger.info("Building quantum circuits.")
    layers = [generate_nth_layer(n, ENGINE) for n in range(NUM_LAYERS)]
    logger.info("Done.")

    # Using the Adam optimizer.
    optimizer = tf.keras.optimizers.Adam(learning_rate=0.001)

    # Training loop.
    for _ in range(NUM_ITERATIONS):
        start = time.time()
        step()
        end = time.time()
        elapsed = (end - start) / 60.0
        print(f"Step took {elapsed} seconds.")
