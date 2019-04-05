import logging
import os
from time import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from fonduer.learning.disc_models.modules.loss import SoftCrossEntropyLoss
from fonduer.learning.utils import GPU, non_cuda, save_marginals
from sklearn.metrics import roc_auc_score


class Learner(nn.Module):
    def __init__(self, model, name=None):
        nn.Module.__init__(self)
        self.logger = logging.getLogger(__name__)
        self.name = name or self.__class__.__name__
        self.model = model
        self.settings = {}

    def _set_random_seed(self, seed):
        self.seed = seed
        # Set random seed for all numpy operations
        self.rand_state = np.random.RandomState(seed=seed)
        np.random.seed(seed=seed)

        # Set random seed for PyTorch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)

    def _setup_model_loss(self, lr):
        """
        Setup loss and optimizer for PyTorch model.
        """
        # Setup loss
        if not hasattr(self, "loss"):
            self.loss = SoftCrossEntropyLoss()

        # Setup optimizer
        if not hasattr(self, "optimizer"):
            self.optimizer = optim.Adam(self.model.parameters(), lr=lr)

    def train(
        self,
        X_train,
        Y_train,
        n_epochs=25,
        lr=0.01,
        batch_size=256,
        shuffle=True,
        X_dev=None,
        Y_dev=None,
        pos_label=1,
        print_freq=5,
        dev_ckpt=True,
        dev_ckpt_delay=0.75,
        save_dir="checkpoints",
        seed=1234,
        host_device="CPU",
    ):
        """
        Generic training procedure for PyTorch model

        :param X_train: The training data which is a (list of Candidate objects,
            a sparse matrix of corresponding features) pair.
        :type X_train: pair
        :param Y_train: Array of marginal probabilities for each Candidate.
        :type Y_train: list or numpy.array
        :param n_epochs: Number of training epochs.
        :type n_epochs: int
        :param lr: Learning rate.
        :type lr: float
        :param batch_size: Batch size for learning model.
        :type batch_size: int
        :param shuffle: If True, shuffle training data every epoch.
        :type shuffle: bool
        :param X_dev: Candidates for evaluation, same format as X_train.
        :param Y_dev: Labels for evaluation, same format as Y_train.
        :param print_freq: number of epochs at which to print status, and if present,
            evaluate the dev set (X_dev, Y_dev).
        :type print_freq: int
        :param dev_ckpt: If True, save a checkpoint whenever highest score
            on (X_dev, Y_dev) reached. Note: currently only evaluates at
            every @print_freq epochs.
        :param dev_ckpt_delay: Start dev checkpointing after this portion
            of n_epochs.
        :type dev_ckpt_delay: float
        :param save_dir: Save dir path for checkpointing.
        :type save_dir: str
        :param seed: Random seed
        :type seed: int
        :param host_device: Host device
        :type host_device: str
        """

        # Set model parameters
        self.settings = {
            "n_epochs": n_epochs,
            "lr": lr,
            "batch_size": batch_size,
            "shuffle": shuffle,
            "seed": 1234,
            "host_device": host_device,
        }

        # Set random seed
        self._set_random_seed(self.settings["seed"])

        # self.model._check_input(X_train)
        verbose = print_freq > 0

        # Check the number of classes of the model with training data
        if self.model.settings["num_classes"] != Y_train.shape[1]:
            raise ValueError(
                f"Number of classes doesn't match! Got {Y_train.shape[1]} classes "
                f"from training data while {self.model.name} has "
                f"{self.model.settings['num_classes']}"
            )

        self.cardinality = self.model.settings["num_classes"]

        # Remove unlabeled examples
        diffs = Y_train.max(axis=1) - Y_train.min(axis=1)
        train_idxs = np.where(diffs > 1e-6)[0]

        train_dataloader = self.model._dataloader(
            X=X_train,
            Y=Y_train,
            batch_size=self.settings["batch_size"],
            shuffle=self.settings["shuffle"],
            idxs=train_idxs,
            train=True,
        )

        if X_dev is not None:
            # dev_dataloader = self.model._dataloader(X=X_dev, Y=Y_dev,
            # batch_size=self.settings["batch_size"], shuffle=False, train=False)
            _X_dev, _Y_dev = self.model._preprocess_data(X_dev, Y_dev)

        if self.settings["host_device"] in GPU:
            if not torch.cuda.is_available():
                self.settings["host_device"] = "CPU"
                self.logger.info("GPU is not available, switching to CPU...")
            else:
                self.logger.info("Using GPU...")

        self.model.settings["host_device"] = self.settings["host_device"]

        self.logger.info(f"Settings: {self.settings}")

        # Build network
        # self._build_model()
        self._setup_model_loss(self.settings["lr"])

        # Set up GPU if necessary
        if self.settings["host_device"] in GPU:
            nn.Module.cuda(self)
            self.model.set_cuda()

        # Run mini-batch SGD
        n = len(train_dataloader.dataset)
        if self.settings["batch_size"] > n:
            self.logger.info(f"Switching batch size to {n} for training.")
        batch_size = min(self.settings["batch_size"], n)

        if verbose:
            st = time()
            self.logger.info(f"[{self.name}] Training model")
            self.logger.info(
                f"[{self.name}] "
                f"n_train={n} "
                f"#epochs={self.settings['n_epochs']} "
                f"batch size={batch_size}"
            )

        dev_score_opt = 0.0
        dev_score_epo = -1
        for epoch in range(self.settings["n_epochs"]):
            iteration_losses = []

            self.model.train_mode()

            for X_batch, Y_batch in train_dataloader:
                # zero gradients for each batch
                self.optimizer.zero_grad()

                output = self.model._calc_logits(X_batch)
                # print(type(output), type(Y_batch))
                # print(output, Y_batch)
                loss = self.loss(output, Y_batch)

                # Compute gradient
                loss.backward()

                # Update the parameters
                self.optimizer.step()

                iteration_losses.append(non_cuda(loss))

            # Print training stats and optionally checkpoint model
            if verbose and (
                (
                    (epoch + 1) % print_freq == 0
                    or epoch in [0, (self.settings["n_epochs"] - 1)]
                )
            ):
                msg = (
                    f"[{self.name}] "
                    f"Epoch {epoch + 1} ({time() - st:.2f}s)\t"
                    f"Average loss={torch.stack(iteration_losses).mean():.6f}"
                )
                if X_dev is not None:
                    scores = self.score(_X_dev, _Y_dev, pos_label=pos_label)

                    score = scores["accuracy"] if self.cardinality > 2 else scores["f1"]
                    score_label = "Acc." if self.cardinality > 2 else "F1"
                    msg += f"\tDev {score_label}={100.0 * score:.2f}"
                    print(scores)
                self.logger.info(msg)

                # If best score on dev set so far and dev checkpointing is
                # active, save checkpoint
                if (
                    X_dev is not None
                    and dev_ckpt
                    and epoch > dev_ckpt_delay * self.settings["n_epochs"]
                    and score > dev_score_opt
                ):
                    dev_score_opt = score
                    dev_score_epo = epoch
                    self.save(save_dir=save_dir, global_step=dev_score_epo)

        # Conclude training
        if verbose:
            self.logger.info(f"[{self.name}] Training done ({time() - st:.2f}s)")
        # If checkpointing on, load last checkpoint (i.e. best on dev set)
        if dev_ckpt and X_dev is not None and verbose and dev_score_opt > 0:
            self.logger.info("Loading best checkpoint")
            self.load(save_dir=save_dir, global_step=dev_score_epo)

    def marginals(self, X):
        """
        Compute the marginals for the given candidates X.
        Note: split into batches to avoid OOM errors.

        :param X: The input data which is a (list of Candidate objects, a sparse
            matrix of corresponding features) pair or a list of
            (Candidate, features) pairs.
        :type X: pair or list
        """

        self.model.eval_mode()
        dataloader = self.model._dataloader(
            X=X, batch_size=self.settings["batch_size"], shuffle=False
        )

        # if self.model._check_input(X):
        #     X = self.model._preprocess_data(X)

        # dataloader = DataLoader(
        #     MultiModalDataset(X),
        #     batch_size=self.settings["batch_size"],
        #     collate_fn=self.model._collate_fn(),
        #     shuffle=False,
        # )

        marginals = torch.Tensor([])

        for X_batch in dataloader:
            # print(X_batch)
            marginal = non_cuda(self.model._calc_logits(X_batch))
            marginals = torch.cat((marginals, marginal), 0)

        return F.softmax(marginals, dim=-1).detach().numpy()

    def save_marginals(self, session, X, training=False):
        """Save the predicted marginal probabilities for the Candidates X.

        :param session: The database session to use.
        :param X: Input data.
        :param training: If True, these are training marginals / labels;
            else they are saved as end model predictions.
        :type training: bool
        """

        save_marginals(session, X, self.marginals(X), training=training)

    def predict(self, X, b=0.5, pos_label=1, return_probs=False):
        """Return numpy array of class predictions for X
        based on predicted marginal probabilities.

        :param X: Input data.
        :param b: Decision boundary *for binary setting only*.
        :type b: float
        :param pos_label: Positive class index *for binary setting only*. Default: 1
        :type pos_label: int
        :param return_probs: If True, return predict probability. Default: False
        :type return_probs: bool
        """

        if self.model._check_input(X):
            X, _ = self.model._preprocess_data(X)

        Y_prob = self.marginals(X)

        if self.model.settings["num_classes"] > 2:
            Y_pred = Y_prob.argmax(axis=1) + 1
            if return_probs:
                return Y_pred, Y_prob
            else:
                return Y_pred

        if pos_label not in [1, 2]:
            raise ValueError("pos_label must have values in {1,2}.")
        self.logger.info(f"Using positive label class {pos_label} with threshold {b}")

        Y_pred = np.array(
            [pos_label if p[pos_label - 1] > b else 3 - pos_label for p in Y_prob]
        )
        if return_probs:
            return Y_pred, Y_prob
        else:
            return Y_pred

    def score(
        self, X_test, Y_test, b=0.5, pos_label=1, set_unlabeled_as_neg=True, beta=1
    ):
        """
        Returns the summary scores:
            * For binary: precision, recall, F-beta score, ROC-AUC score
            * For categorical: accuracy

        :param X_test: The input test candidates.
        :type X_test: pair with candidates and corresponding features
        :param Y_test: The input test labels.
        :type Y_test: list of labels
        :param b: Decision boundary *for binary setting only*.
        :type b: float
        :param pos_label: Positive class index *for binary setting only*. Default: 1
        :type pos_label: int
        :param set_unlabeled_as_neg: Whether to map 0 labels -> -1,
            *for binary setting only*
        :type set_unlabeled_as_neg: bool
        :param beta: For F-beta score; by default beta = 1 => F-1 score.
        :type beta: int
        """

        if self.model._check_input(X_test):
            X_test, Y_test = self.model._preprocess_data(X_test, Y_test)

        Y_pred, Y_prob = self.predict(
            X_test, b=b, pos_label=pos_label, return_probs=True
        )

        # Convert Y_test to dense numpy array
        try:
            Y_test = np.array(Y_test.todense()).reshape(-1)
        except Exception:
            Y_test = np.array(Y_test)

        scores = {}

        # Compute P/R/F1 for binary settings
        if self.model.settings["num_classes"] == 2:
            # Either remap or filter out unlabeled (0-valued) test labels
            if set_unlabeled_as_neg:
                Y_test[Y_test == 0] = 3 - pos_label
            else:
                Y_pred = Y_pred[Y_test != 0]
                Y_test = Y_test[Y_test != 0]

            # Compute and return precision, recall, and F1 score

            pred_pos = np.where(Y_pred == pos_label, True, False)
            gt_pos = np.where(Y_test == pos_label, True, False)
            TP = np.sum(pred_pos * gt_pos)
            FP = np.sum(pred_pos * np.logical_not(gt_pos))
            FN = np.sum(np.logical_not(pred_pos) * gt_pos)

            prec = TP / (TP + FP) if TP + FP > 0 else 0.0
            rec = TP / (TP + FN) if TP + FN > 0 else 0.0
            fbeta = (
                (1 + beta ** 2) * (prec * rec) / ((beta ** 2 * prec) + rec)
                if (beta ** 2 * prec) + rec > 0
                else 0.0
            )
            scores["precision"] = prec
            scores["recall"] = rec
            scores[f"f{beta}"] = fbeta

            roc_auc = roc_auc_score(Y_test, Y_prob[:, pos_label - 1])
            scores["roc_auc"] = roc_auc

        # Compute accuracy for all settings
        acc = np.where([Y_pred == Y_test])[0].shape[0] / float(Y_test.shape[0])
        scores["accuracy"] = acc

        return scores

    def save(
        self, model_name=None, save_dir="checkpoints", verbose=True, global_step=0
    ):
        """Save current model.

        :param model_name: Saved model name.
        :type model_name: str
        :param save_dir: Saved model directory.
        :type save_dir: str
        :param verbose: Print log or not
        :type verbose: bool
        :param global_step: learned epoch of saved model
        :type global_step: int
        """

        model_name = model_name or self.name

        # Check existence of model saving directory and create if does not exist.
        model_dir = os.path.join(save_dir, model_name)
        if not os.path.exists(model_dir):
            os.makedirs(model_dir)

        params = {
            "model": self.model.state_dict(),
            "cardinality": self.cardinality,
            "name": model_name,
            "config": self.settings,
            "epoch": global_step,
        }

        model_file = f"{model_name}.mdl.ckpt.{global_step}"

        try:
            torch.save(params, f"{model_dir}/{model_file}")
        except BaseException:
            self.logger.warning("Saving failed... continuing anyway.")

        if verbose:
            self.logger.info(
                f"[{model_name}] Model saved as {model_file} in {model_dir}"
            )

    def load(
        self, model_name=None, save_dir="checkpoints", verbose=True, global_step=0
    ):
        """Load model from file and rebuild the model.

        :param model_name: Saved model name.
        :type model_name: str
        :param save_dir: Saved model directory.
        :type save_dir: str
        :param verbose: Print log or not
        :type verbose: bool
        :param global_step: learned epoch of saved model
        :type global_step: int
        """

        model_name = model_name or self.name

        model_dir = os.path.join(save_dir, model_name)
        if not os.path.exists(model_dir):
            self.logger.error("Loading failed... Directory does not exist.")

        model_file = f"{model_name}.mdl.ckpt.{global_step}"

        try:
            checkpoint = torch.load(f"{model_dir}/{model_file}")
        except BaseException:
            self.logger.error(
                f"Loading failed... Cannot load model from {model_name} in {model_dir}"
            )

        self.model.load_state_dict(checkpoint["model"])
        self.settings = checkpoint["config"]
        self.cardinality = checkpoint["cardinality"]
        self.name = checkpoint["name"]

        if verbose:
            self.logger.info(
                f"[{model_name}] Model loaded as {model_file} in {model_dir}"
            )