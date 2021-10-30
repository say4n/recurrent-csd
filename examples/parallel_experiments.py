from multiprocessing import Pool, cpu_count
from typing import Iterator, List, NamedTuple
from csd import CSD
from csd.plot import Plot
from csd.typings.typing import (MeasuringTypes, CSDConfiguration, Backends,
                                OneProcessResultExecution, ResultExecution, RunConfiguration)
import numpy as np
from csd.util import timing
import os
# from csd.config import logger


class MultiProcessConfiguration(NamedTuple):
    alphas: List[float]
    steps: List[int]
    learning_rate: List[float]
    batch_size: List[int]
    shots: List[int]
    plays: List[int]
    cutoff_dim: List[int]
    number_modes: List[int]
    number_layers: List[int]
    squeezing: List[bool]


class LaunchExecutionConfiguration(NamedTuple):
    backend: Backends
    measuring_type: MeasuringTypes
    steps: int
    learning_rate: float
    batch_size: int
    shots: int
    plays: int
    cutoff_dim: int
    number_modes: int
    number_layers: int
    squeezing: bool
    alpha: float


def _set_plot_label(backend: Backends, measuring_type: MeasuringTypes) -> str:
    """Set the label for the success probability plot

    Args:
        backend (Backends): Current experiment backend
        measuring_type (MeasuringTypes): Current experiment measuring type

    Returns:
        str: the determined label
    """
    if backend is Backends.FOCK and measuring_type is MeasuringTypes.PROBABILITIES:
        return "pFockProb(a)"
    if backend is Backends.GAUSSIAN and measuring_type is MeasuringTypes.PROBABILITIES:
        return "pGausProb(a)"
    if backend is Backends.TENSORFLOW and measuring_type is MeasuringTypes.PROBABILITIES:
        return "pTFProb(a)"
    if backend is Backends.FOCK and measuring_type is MeasuringTypes.SAMPLING:
        return "pFockSampl(a)"
    if backend is Backends.TENSORFLOW and measuring_type is MeasuringTypes.SAMPLING:
        return "pTFSampl(a)"
    if backend is Backends.GAUSSIAN and measuring_type is MeasuringTypes.SAMPLING:
        return "pGausSampl(a)"
    raise ValueError(f"Values not supported. backend: {backend.value} and measuring_type: {measuring_type.value}")


def _set_plot_title(backend: Backends,
                    measuring_type: MeasuringTypes,
                    batch_size: int,
                    plays: int,
                    modes: int,
                    layers: int,
                    squeezing: bool) -> str:

    return (f"backend:{backend.value}, "
            f"measuring:{measuring_type.value}, \n"
            f"batch size:{batch_size}, plays:{plays}, modes:{modes}, layers,{layers}, squeezing:{squeezing}")


@timing
def launch_execution(configuration: LaunchExecutionConfiguration) -> ResultExecution:
    csd = CSD(csd_config=CSDConfiguration({
        'alphas': [configuration.alpha],
        'steps': configuration.steps,
        'learning_rate': configuration.learning_rate,
        'batch_size': configuration.batch_size,
        'shots': configuration.shots,
        'plays': configuration.plays,
        'cutoff_dim': configuration.cutoff_dim,
        'architecture': {
            'number_modes': configuration.number_modes,
            'number_layers': configuration.number_layers,
            'squeezing': configuration.squeezing,
        },
        'save_results': False,
        'save_plots': False
    }))
    return csd.execute(configuration=RunConfiguration({
        'backend': configuration.backend,
        'measuring_type': configuration.measuring_type,
    }))


def uncurry_launch_execution(t) -> ResultExecution:
    one_execution_configuration = LaunchExecutionConfiguration(
        backend=t[0],
        measuring_type=t[1],
        steps=t[2],
        learning_rate=t[3],
        batch_size=t[4],
        shots=t[5],
        plays=t[6],
        cutoff_dim=t[7],
        number_modes=t[8],
        number_layers=t[9],
        squeezing=t[10],
        alpha=t[11],
    )
    return launch_execution(configuration=one_execution_configuration)


def update_execution_result(acumulated_one_process_result: OneProcessResultExecution,
                            input_result: ResultExecution) -> OneProcessResultExecution:
    new_one_process_result = acumulated_one_process_result.copy()

    for opt_param, p_err, p_succ in zip(input_result['opt_params'], input_result['p_err'], input_result['p_succ']):
        new_one_process_result['opt_params'].append(opt_param)
        new_one_process_result['p_err'].append(p_err)
        new_one_process_result['p_succ'].append(p_succ)

    return new_one_process_result


def create_full_execution_result(backend: Backends,
                                 measuring_type: MeasuringTypes,
                                 multiprocess_configuration: MultiProcessConfiguration,
                                 results: List[ResultExecution]) -> ResultExecution:
    acumulated_one_process_result = OneProcessResultExecution({
        'opt_params': [],
        'p_err': [],
        'p_succ': []
    })
    for result in results:
        acumulated_one_process_result = update_execution_result(
            acumulated_one_process_result=acumulated_one_process_result,
            input_result=result)

    return ResultExecution({
        'alphas': multiprocess_configuration.alphas,
        'batches': [],
        'opt_params': acumulated_one_process_result['opt_params'],
        'p_err': acumulated_one_process_result['p_err'],
        'p_succ': acumulated_one_process_result['p_succ'],
        'backend': backend.value,
        'measuring_type': measuring_type.value,
        'plot_label': _set_plot_label(backend=backend,
                                      measuring_type=measuring_type),
        'plot_title': _set_plot_title(backend=backend,
                                      measuring_type=measuring_type,
                                      batch_size=multiprocess_configuration.batch_size[0],
                                      plays=multiprocess_configuration.plays[0],
                                      modes=multiprocess_configuration.number_modes[0],
                                      layers=multiprocess_configuration.number_layers[0],
                                      squeezing=multiprocess_configuration.squeezing[0])
    })


def plot_results(alphas: List[float], execution_result: ResultExecution) -> None:
    plot = Plot(alphas=alphas)
    plot.plot_success_probabilities(executions=[execution_result], save_plot=True)


@timing
def _general_execution(multiprocess_configuration: MultiProcessConfiguration,
                       backend: Backends,
                       measuring_type: MeasuringTypes):

    pool = Pool(processes=5 if backend == Backends.TENSORFLOW else cpu_count())
    execution_results = pool.map_async(func=uncurry_launch_execution,
                                       iterable=_build_iterator(multiprocess_configuration,
                                                                backend,
                                                                measuring_type)).get()

    result = create_full_execution_result(backend=backend,
                                          measuring_type=measuring_type,
                                          multiprocess_configuration=multiprocess_configuration,
                                          results=execution_results)

    pool.close()
    pool.join()

    plot_results(alphas=multiprocess_configuration.alphas,
                 execution_result=result)


def _build_iterator(multiprocess_configuration: MultiProcessConfiguration,
                    backend: Backends,
                    measuring_type: MeasuringTypes) -> Iterator:
    return zip([backend] * batch_size,
               [measuring_type] * batch_size,
               multiprocess_configuration.steps,
               multiprocess_configuration.learning_rate,
               multiprocess_configuration.batch_size,
               multiprocess_configuration.shots,
               multiprocess_configuration.plays,
               multiprocess_configuration.cutoff_dim,
               multiprocess_configuration.number_modes,
               multiprocess_configuration.number_layers,
               multiprocess_configuration.squeezing,
               multiprocess_configuration.alphas)


def multi_fock_backend(multiprocess_configuration: MultiProcessConfiguration) -> None:

    backend = Backends.FOCK
    measuring_type = MeasuringTypes.PROBABILITIES

    _general_execution(multiprocess_configuration=multiprocess_configuration,
                       backend=backend,
                       measuring_type=measuring_type)


def multi_tf_backend(multiprocess_configuration: MultiProcessConfiguration) -> None:
    os.environ["TF_FORCE_GPU_ALLOW_GROWTH"] = "true"

    backend = Backends.TENSORFLOW
    measuring_type = MeasuringTypes.PROBABILITIES

    _general_execution(multiprocess_configuration=multiprocess_configuration,
                       backend=backend,
                       measuring_type=measuring_type)


if __name__ == '__main__':
    alphas = list(np.arange(0.05, 1.05, 0.05))
    # alphas = [0.7]'steps': 300,

    steps = 300
    learning_rate = 0.1
    batch_size = 1000
    shots = 100
    plays = 1
    cutoff_dim = 10
    number_modes = 2
    number_layers = 1
    squeezing = False

    number_alphas = len(alphas)

    multiprocess_configuration = MultiProcessConfiguration(
        alphas=alphas,
        steps=[steps] * number_alphas,
        learning_rate=[learning_rate] * number_alphas,
        batch_size=[batch_size] * number_alphas,
        shots=[shots] * number_alphas,
        plays=[plays] * number_alphas,
        cutoff_dim=[cutoff_dim] * number_alphas,
        number_modes=[number_modes] * number_alphas,
        number_layers=[number_layers] * number_alphas,
        squeezing=[squeezing] * number_alphas
    )

    # multi_tf_backend(multiprocess_configuration=multiprocess_configuration)
    multi_fock_backend(multiprocess_configuration=multiprocess_configuration)