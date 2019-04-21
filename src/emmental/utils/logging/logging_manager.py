import logging

from emmental.utils.logging.checkpointer import Checkpointer
from emmental.utils.logging.log_writer import LogWriter
from emmental.utils.logging.tensorboard_writer import TensorBoardWriter

logger = logging.getLogger(__name__)


class LoggingManager(object):
    """A class to manage logging during training progress

    :param config: the config object for counter
    :type config: dict
    :param n_batches_per_epoch: total number batches per epoch
    :type n_batches_per_epoch: int
    :param verbose: print out the log or not
    :type verbose: bool
    """

    def __init__(self, config, n_batches_per_epoch):
        self.n_batches_per_epoch = n_batches_per_epoch

        # Set up counter

        # Set up evaluation/checkpointing unit (sample, batch, epoch)
        self.counter_unit = config["logging_config"]["counter_unit"]

        if self.counter_unit not in ["sample", "batch", "epoch"]:
            raise ValueError(f"Unrecognized unit: {self.counter_unit}")

        # Set up evaluation frequency
        self.evaluation_freq = config["logging_config"]["evaluation_freq"]
        logger.info(f"Evaluating every {self.evaluation_freq} {self.counter_unit}.")

        # Set up checkpointing frequency
        self.checkpointing_freq = int(
            config["logging_config"]["checkpointer_config"]["checkpoint_freq"]
        )
        logger.info(
            f"Checkpointing every "
            f"{self.checkpointing_freq * self.evaluation_freq} {self.counter_unit}."
        )

        # Set up number of samples passed since last evaluation/checkpointing and
        # total number of samples passed since learning process
        self.sample_count = 0
        self.sample_total = 0

        # Set up number of batches passed since last evaluation/checkpointing and
        # total number of batches passed since learning process
        self.batch_count = 0
        self.batch_total = 0

        # Set up number of epochs passed since last evaluation/checkpointing and
        # total number of epochs passed since learning process
        self.epoch_count = 0
        self.epoch_total = 0

        # Set up number of unit passed since last evaluation/checkpointing and
        # total number of unit passed since learning process
        self.unit_count = 0
        self.unit_total = 0

        # Set up count that triggers the evaluation since last checkpointing
        self.trigger_count = 0

        # Set up log writer
        writer_opt = config["logging_config"]["writer_config"]["writer"]

        if writer_opt is None:
            self.writer = None
        elif writer_opt == "json":
            self.writer = LogWriter()
        elif writer_opt == "tensorboard":
            self.writer = TensorBoardWriter()
        else:
            raise ValueError(f"Unrecognized writer option '{writer_opt}'")

        # Set up checkpointer
        # checkpointer_config = config["logging_config"]["checkpointer_config"]
        self.checkpointer = Checkpointer(config)

    def update(self, batch_size):
        """Update the count and total number"""

        # Update number of samples
        self.sample_count += batch_size
        self.sample_total += batch_size

        # Update number of batches
        self.batch_count += 1
        self.batch_total += 1

        # Update number of epochs
        self.epoch_count = self.batch_count / self.n_batches_per_epoch
        self.epoch_total = self.batch_total / self.n_batches_per_epoch

        # Update number of units
        if self.counter_unit == "sample":
            self.unit_count = self.sample_count
            self.unit_total = self.sample_total
        if self.counter_unit == "batch":
            self.unit_count = self.batch_count
            self.unit_total = self.batch_total
        elif self.counter_unit == "epoch":
            self.unit_count = self.epoch_count
            self.unit_total = self.epoch_total

    def trigger_evaluation(self):
        """Check if triggers the evaluation"""
        satisfied = self.unit_count >= self.evaluation_freq
        if satisfied:
            self.trigger_count += 1
            self.reset()
        return satisfied

    def trigger_checkpointing(self):
        """Check if triggers the checkpointing"""
        satisfied = self.trigger_count >= self.checkpointing_freq
        if satisfied:
            self.trigger_count = 0
        return satisfied

    def reset(self):
        """Reset the counter"""
        self.sample_count = 0
        self.batch_count = 0
        self.epoch_count = 0
        self.unit_count = 0

    def write_log(self, metric_dict):
        for metric_name, metric_value in metric_dict.items():
            self.writer.add_scalar(metric_name, metric_value, self.unit_total)

    def checkpoint_model(self, model, optimizer, lr_scheduler, metric_dict):
        self.checkpointer.checkpoint(
            self.unit_total, model, optimizer, lr_scheduler, metric_dict
        )