from abc import ABC
from csd.circuit import Circuit
from csd.engine import Engine
from csd.typings import (Backends, CSDConfiguration, EngineRunOptions,
                         RunConfiguration,
                         MeasuringTypes,
                         PhotodetectorProbabilities,
                         ResultExecution,
                         Architecture)
from strawberryfields.api import Result
from strawberryfields.backends import BaseState
from typing import Optional, Union, cast, List
import numpy as np
import random
from csd.config import logger
from tensorflow.python.framework.ops import EagerTensor
from csd.optimize import Optimize
from csd.plot import Plot
from csd.util import timing, save_object_to_disk


class CSD(ABC):
    A = 1
    MINUS_A = -1
    DEFAULT_NUM_SHOTS = 100
    DEFAULT_CUTOFF_DIMENSION = 2
    DEFAULT_ALPHA = 0.7
    DEFAULT_BATCH_SIZE = 10
    DEFAULT_STEPS: int = 100
    DEFAULT_LEARNING_RATE: float = 0.1

    def __init__(self, csd_config: Union[CSDConfiguration, None] = None):
        self._set_default_values()

        if csd_config is not None:
            self._set_values_from_config(csd_config)

        self._set_default_values_after_config()

    def _set_default_values_after_config(self):
        self._architecture = self._set_architecture(self._architecture).copy()

    def _set_values_from_config(self, csd_config):
        self._steps = csd_config.get('steps', self.DEFAULT_STEPS)
        self._learning_rate = csd_config.get('learning_rate', self.DEFAULT_LEARNING_RATE)
        self._batch_size = csd_config.get('batch_size', self.DEFAULT_BATCH_SIZE)
        self._shots = csd_config.get('shots', self.DEFAULT_NUM_SHOTS)
        self._cutoff_dim = csd_config.get('cutoff_dim', self.DEFAULT_CUTOFF_DIMENSION)
        self._batch = csd_config.get('batch', [])
        self._architecture = self._set_architecture(csd_config.get('architecture')).copy()
        self._save_results = csd_config.get('save_results', False)
        self._save_plots = csd_config.get('save_plots', False)

    def _set_default_values(self):
        self._shots = self.DEFAULT_NUM_SHOTS
        self._batch_size = self.DEFAULT_BATCH_SIZE
        self._cutoff_dim = self.DEFAULT_CUTOFF_DIMENSION
        self._steps = self.DEFAULT_STEPS
        self._learning_rate = self.DEFAULT_LEARNING_RATE
        self._result = None
        self._probability_results: List[ResultExecution] = []
        self._sampling_results: List[ResultExecution] = []
        self._plot: Union[Plot, None] = None
        self._circuit: Union[Circuit, None] = None
        self._run_configuration: Union[RunConfiguration, None] = None
        self._alphas: Union[List[float], None] = None
        self._save_results = False
        self._save_plots = False

    def _set_architecture(self, architecture: Optional[Architecture] = None) -> Architecture:
        tmp_architecture = self._default_architecture()
        if architecture is None:
            return tmp_architecture
        if 'number_qumodes' in architecture:
            tmp_architecture['number_qumodes'] = architecture['number_qumodes']
        if 'number_layers' in architecture:
            tmp_architecture['number_layers'] = architecture['number_layers']
        if 'displacement' in architecture:
            tmp_architecture['displacement'] = architecture['displacement']
        if 'squeezing' in architecture:
            tmp_architecture['squeezing'] = architecture['squeezing']
        if 'interferometer' in architecture:
            tmp_architecture['interferometer'] = architecture['interferometer']
        return tmp_architecture

    def _default_architecture(self) -> Architecture:
        return {
            'number_qumodes': 1,
            'number_layers': 1,
            'displacement': True,
            'squeezing': False,
            'interferometer': False
        }

    def _create_batch(self, samples: List[float], batch_size=10) -> List[float]:
        return [random.choice(samples) for _ in range(batch_size)]

    def _create_input_batch(self, batch: List[float], alpha_value: float) -> List[float]:
        return list(alpha_value * np.array(batch))

    def _create_random_batch(self, batch_size=10, alpha_value: float = 0.7) -> List[float]:
        base_batch = self._create_batch(samples=[self.A, self.MINUS_A], batch_size=batch_size)
        return self._create_input_batch(batch=base_batch, alpha_value=alpha_value)

    def _compute_photodetector_probabilities(self,
                                             batch: List[float],
                                             params: List[float]) -> PhotodetectorProbabilities:
        no_click_probabilities = self._compute_no_click_probabilities(batch, params)
        if isinstance(no_click_probabilities, EagerTensor):
            return {
                'prob_click': 1 - no_click_probabilities,
                'prob_no_click': no_click_probabilities
            }
        return {
            'prob_click': list(1 - np.array(no_click_probabilities)),
            'prob_no_click': list(no_click_probabilities)
        }

    def _compute_no_click_probabilities(self,
                                        batch: List[float],
                                        params: List[float]) -> Union[List[float], EagerTensor]:
        if self._circuit is None:
            raise ValueError("Circuit must be initialized")
        if self._run_configuration is None:
            raise ValueError("Run configuration not specified")

        if 'shots' in self._run_configuration:
            self._shots = self._run_configuration['shots']

        if self._engine.backend_name == Backends.TENSORFLOW.value:
            return self._engine.run_circuit_checking_measuring_type(
                circuit=self._circuit,
                options=EngineRunOptions(
                    params=params,
                    sample_or_batch=batch,
                    shots=self._shots,
                    measuring_type=self._run_configuration['measuring_type']
                ))

        return [cast(float, self._engine.run_circuit_checking_measuring_type(
            circuit=self._circuit,
            options=EngineRunOptions(
                params=params,
                sample_or_batch=sample,
                shots=self._shots,
                measuring_type=self._run_configuration['measuring_type']
            )))
            for sample in batch]

    def _compute_average_error_probability(self,
                                           batch: List[float],
                                           photodetector_prob: PhotodetectorProbabilities) -> float:

        p_errors = [photodetector_prob['prob_click'][batch_index] if sample == self._alpha_value
                    else photodetector_prob['prob_no_click'][batch_index]
                    for batch_index, sample in enumerate(batch)]

        return sum(p_errors) / len(batch)

    def _create_circuit(self) -> Circuit:
        """Creates a circuit to run an experiment based on configuration parameters

        Returns:
            sf.Program: the created circuit
        """

        if self._run_configuration is None:
            raise ValueError("Run configuration not specified")
        if not self._architecture:
            raise ValueError('No architecture specified')

        return Circuit(architecture=self._architecture,
                       measuring_type=self._run_configuration['measuring_type'])

    def _cost_function(self, params: List[float]) -> Union[float, EagerTensor]:
        photodetector_probabilities = self._compute_photodetector_probabilities(batch=self._batch,
                                                                                params=params)

        p_err = self._compute_average_error_probability(batch=self._batch,
                                                        photodetector_prob=photodetector_probabilities)
        self._current_p_err = self._save_current_p_error(p_err)
        return p_err

    def _save_current_p_error(self, p_err: Union[float, EagerTensor]) -> float:
        if isinstance(p_err, EagerTensor):
            return float(p_err.numpy())
        return p_err

    @timing
    def execute(self, configuration: RunConfiguration) -> ResultExecution:
        """Run an experiment for the same batch with the given configuration

        Args:
            configuration (RunConfiguration): Specific experiment configuration

        Returns:
            float: probability of getting |0> state
        """
        if not self._architecture:
            raise ValueError('No architecture specified')
        if self._architecture['number_layers'] != 1 or self._architecture['number_qumodes'] != 1:
            raise ValueError('Experiment only available for ONE qumode and ONE layer')
        self._run_configuration = configuration.copy()
        self._batch_size = (configuration['batch_size'] if 'batch_size' in configuration
                            else self._batch_size)
        self._alphas = configuration['alphas'] if 'alphas' in configuration else [self.DEFAULT_ALPHA]

        self._circuit = self._create_circuit()
        self._engine = self._create_engine()

        learning_steps = self._set_learning_steps()
        optimization = Optimize(backend=self._run_configuration['backend'])

        logger.debug(f"Executing One Layer circuit with Backend: {self._run_configuration['backend'].value}, "
                     " with measuring_type: "
                     f"{cast(MeasuringTypes, self._run_configuration['measuring_type']).value}"
                     f" and cutoff_dim: {self._cutoff_dim}")

        result = self._init_result()

        for sample_alpha in self._alphas:
            self._execute_for_one_alpha(learning_steps, optimization, result, sample_alpha)

        self._save_results_to_file(result)
        self._save_plot_to_file(result)

        return result

    def _save_plot_to_file(self, result):
        if self._save_plots:
            if self._plot is None:
                self._plot = Plot(alphas=self._alphas)
            logger.info("Save plot to file")
            self._plot.plot_success_probabilities(executions=[result], save_plot=True)

    def _save_results_to_file(self, result):
        if self._save_results:
            logger.info("Save results to file")
            save_object_to_disk(obj=result, path='results')

    @timing
    def _execute_for_one_alpha(self, learning_steps, optimization, result, sample_alpha):
        self._alpha_value = sample_alpha
        logger.debug(f'Optimizing for alpha: {np.round(self._alpha_value, 2)}')

        for step in range(learning_steps):
            optimized_parameters = self._execute_for_one_alpha_and_step(batch_size=self._batch_size,
                                                                        optimization=optimization)
            self._print_opimized_parameters(step, optimized_parameters)
        self._update_result(result, optimized_parameters)

    def _print_opimized_parameters(self, step, optimized_parameters):
        if self._backend_is_tf() and (step + 1) % 100 == 0:
            logger.debug("Learned parameters value at step {}: {}".format(
                step + 1, optimized_parameters))

    def _update_result(self, result, optimized_parameters):
        logger.debug(f'Optimized for alpha: {np.round(self._alpha_value, 2)}'
                     f' parameters: {optimized_parameters}'
                     f' p_succ: {1 - self._current_p_err}')
        result['alphas'].append(np.round(self._alpha_value, 2))
        result['batches'].append(self._batch)
        result['opt_params'].append(cast(float, optimized_parameters))
        result['p_err'].append(self._current_p_err)
        result['p_succ'].append(1 - self._current_p_err)

    def _init_result(self):
        result: ResultExecution = {
            'alphas': [],
            'batches': [],
            'opt_params': [],
            'p_err': [],
            'p_succ': [],
            'backend': self._run_configuration['backend'].value,
            'measuring_type': self._run_configuration['measuring_type'].value,
            'plot_label': self._set_plot_label(backend=self._run_configuration['backend'],
                                               measuring_type=self._run_configuration['measuring_type'])
        }

        return result

    def _create_engine(self) -> Engine:
        if self._run_configuration is None:
            raise ValueError("Run configuration not specified")

        cutoff_dim = (self._run_configuration['cutoff_dim']
                      if 'cutoff_dim' in self._run_configuration
                      else self._cutoff_dim)
        return Engine(backend=self._run_configuration['backend'], options={
            "cutoff_dim": cutoff_dim,
            "batch_size": self._batch_size if self._backend_is_tf() else None
        })

    def _backend_is_tf(self):
        return self._run_configuration['backend'] == Backends.TENSORFLOW

    def _set_learning_steps(self) -> int:
        if self._run_configuration is None:
            raise ValueError("Run configuration not specified")

        if self._backend_is_tf():
            steps = (self._steps if self._steps is not None and 'steps' not in self._run_configuration
                     else self._steps)
            return self._run_configuration['steps'] if 'steps' in self._run_configuration else steps
        return 1

    def _execute_for_one_alpha_and_step(self,
                                        batch_size: int,
                                        optimization: Optimize) -> List[float]:

        self._batch = self._create_random_batch(batch_size=batch_size, alpha_value=self._alpha_value)
        optimized_result = optimization.optimize(cost_function=self._cost_function)
        # logger.debug(f'optimized_result: {optimized_result}')
        return optimized_result

    def _set_plot_label(self, backend: Backends, measuring_type: MeasuringTypes) -> str:
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

    @timing
    def execute_all_backends_and_measuring_types(
            self,
            alphas: List[float],
            backends: List[Backends] = [Backends.FOCK,
                                        Backends.GAUSSIAN,
                                        Backends.TENSORFLOW],
            measuring_types: Optional[List[MeasuringTypes]] = [MeasuringTypes.PROBABILITIES]) -> List[ResultExecution]:
        """Execute the experiment for all backends and measuring types

        Args:
            alphas (List[float]): List of input alpha values to generate batches and run the experiments

        Returns:
            List[ResultExecution]: List of the Result Executions
        """
        if measuring_types is None:
            measuring_types = [MeasuringTypes.PROBABILITIES]

        required_probability_execution = measuring_types.count(MeasuringTypes.PROBABILITIES) > 0
        required_sampling_execution = measuring_types.count(MeasuringTypes.SAMPLING) > 0

        if required_probability_execution:
            self._probability_results = self._execute_with_given_backends(alphas=alphas,
                                                                          backends=backends,
                                                                          measuring_type=MeasuringTypes.PROBABILITIES)

        if required_sampling_execution:
            self._sampling_results += self._execute_with_given_backends(alphas=alphas,
                                                                        backends=backends,
                                                                        measuring_type=MeasuringTypes.SAMPLING)
        return self._probability_results + self._sampling_results

    def _execute_with_given_backends(self,
                                     alphas: List[float],
                                     backends: List[Backends],
                                     measuring_type: MeasuringTypes) -> List[ResultExecution]:

        return [self.execute(configuration=RunConfiguration({
            'alphas': alphas,
            'backend': backend,
            'measuring_type': measuring_type,
            'shots': self._shots,
            'batch_size': self._batch_size,
            'cutoff_dim': self._cutoff_dim,
        })) for backend in backends]

    def plot_success_probabilities(self,
                                   alphas: Optional[Union[List[float], None]] = None,
                                   measuring_types: Optional[Union[List[MeasuringTypes], None]] = None) -> None:
        if self._alphas is None and alphas is None:
            raise ValueError("alphas not set. You must execute the optimization first.")
        if self._alphas is None:
            self._alphas = alphas
        if self._plot is None:
            self._plot = Plot(alphas=self._alphas)
        if measuring_types is None:
            self._plot.plot_success_probabilities()
            return None

        if self._probability_results == [] and self._sampling_results == []:
            raise ValueError("results not set.")

        required_probability_execution = measuring_types.count(MeasuringTypes.PROBABILITIES) > 0
        required_sampling_execution = measuring_types.count(MeasuringTypes.SAMPLING) > 0

        if required_probability_execution and required_sampling_execution:
            self._plot.plot_success_probabilities(executions=self._probability_results + self._sampling_results)
            return None
        if required_probability_execution:
            self._plot.plot_success_probabilities(executions=self._probability_results)
            return None
        if required_sampling_execution:
            self._plot.plot_success_probabilities(executions=self._sampling_results)
            return None
        raise ValueError('Value not expected')

    def show_result(self) -> dict:
        if self._result is None:
            raise ValueError("Circuit not executed yet.")
        sf_result = cast(Result, self._result)
        sf_state = cast(BaseState, sf_result.state)

        return {
            'result': str(sf_result),
            'state': str(sf_state),
            'trace': sf_state.trace(),
            'density_matrix': sf_state.dm(),
            'dm_shape': cast(np.ndarray, sf_state.dm()).shape,
            'samples': sf_result.samples,
            'first_sample': sf_result.samples[0],
            'fock_probability': sf_state.fock_prob([0])
        }
