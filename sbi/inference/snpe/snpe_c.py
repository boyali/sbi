from __future__ import annotations

import os

import torch
from torch import distributions
from torch.utils.tensorboard import SummaryWriter

import sbi.utils as utils
from sbi.inference.snpe.snpe_base import SnpeBase


class SnpeC(SnpeBase):
    def __init__(
        self,
        simulator,
        prior,
        true_observation,
        num_atoms=-1,
        num_pilot_samples=100,
        density_estimator=None,
        calibration_kernel=None,
        use_combined_loss=False,
        z_score_obs=True,
        simulation_batch_size: int = 1,
        retrain_from_scratch_each_round=False,
        discard_prior_samples=False,
        summary_writer=None,
        device=None,
        sample_with_mcmc=False,
        mcmc_method="slice-np",
    ):
        """SNPE-C / APT

        Implementation of _Automatic Posterior Transformation for Likelihood-free
        Inference_ by Greenberg et al., ICML 2019, https://arxiv.org/abs/1905.07488

        Args:
            num_pilot_samples: number of simulations that are run when
                instantiating an object. Used to z-score the observations.   
            density_estimator: neural density estimator
            calibration_kernel: a function to calibrate the context
            z_score_obs: whether to z-score the data features x
            use_combined_loss: whether to jointly neural_net prior samples 
                using maximum likelihood. Useful to prevent density leaking when using box uniform priors.
            retrain_from_scratch_each_round: whether to retrain the conditional
                density estimator for the posterior from scratch each round.
            discard_prior_samples: whether to discard prior samples from round
                two onwards.
            num_atoms: int
                Number of atoms to use for classification.
                If -1, use all other parameters in minibatch.
        """

        super(SnpeC, self).__init__(
            simulator=simulator,
            prior=prior,
            true_observation=true_observation,
            num_pilot_samples=num_pilot_samples,
            density_estimator=density_estimator,
            calibration_kernel=calibration_kernel,
            use_combined_loss=use_combined_loss,
            z_score_obs=z_score_obs,
            simulation_batch_size=simulation_batch_size,
            retrain_from_scratch_each_round=retrain_from_scratch_each_round,
            discard_prior_samples=discard_prior_samples,
            device=device,
            sample_with_mcmc=sample_with_mcmc,
            mcmc_method=mcmc_method,
        )

        assert isinstance(num_atoms, int), "Number of atoms must be an integer."
        self._num_atoms = num_atoms

    def _get_log_prob_proposal_posterior(self, inputs, context, masks):
        """
        We have two main options when evaluating the proposal posterior.
        (1) Generate atoms from the proposal prior.
        (2) Generate atoms from a more targeted distribution,
        such as the most recent posterior.
        If we choose the latter, it is likely beneficial not to do this in the first
        round, since we would be sampling from a randomly initialized neural density
        estimator.

        Args:
            inputs: torch.Tensor Batch of parameters.
            context: torch.Tensor Batch of observations.
            masks: torch.Tensor
                binary, whether or not to retrain with prior loss on specific prior sample

        Returns: torch.Tensor [1] log_prob_proposal_posterior

        """

        batch_size = inputs.shape[0]

        num_atoms = self._num_atoms if self._num_atoms > 0 else batch_size

        # Each set of parameter atoms is evaluated using the same observation,
        # so we repeat rows of the context.
        # e.g. [1, 2] -> [1, 1, 2, 2]
        repeated_context = utils.repeat_rows(context, num_atoms)

        # To generate the full set of atoms for a given item in the batch,
        # we sample without replacement num_atoms - 1 times from the rest
        # of the parameters in the batch.
        assert 0 < num_atoms - 1 < batch_size
        probs = (
            (1 / (batch_size - 1))
            * torch.ones(batch_size, batch_size)
            * (1 - torch.eye(batch_size))
        )
        choices = torch.multinomial(probs, num_samples=num_atoms - 1, replacement=False)
        contrasting_inputs = inputs[choices]

        # We can now create our sets of atoms from the contrasting parameter sets
        # we have generated.
        atomic_inputs = torch.cat(
            (inputs[:, None, :], contrasting_inputs), dim=1
        ).reshape(batch_size * num_atoms, -1)

        # Evaluate large batch giving (batch_size * num_atoms) log prob posterior evals.
        log_prob_posterior = self._neural_posterior.log_prob(
            atomic_inputs, repeated_context, normalize_snpe_density=False
        )
        assert torch.isfinite(
            log_prob_posterior
        ).all(), "NaN/inf detected in posterior eval."
        log_prob_posterior = log_prob_posterior.reshape(batch_size, num_atoms)

        # Get (batch_size * num_atoms) log prob prior evals.
        log_prob_prior = self._prior.log_prob(atomic_inputs)
        log_prob_prior = log_prob_prior.reshape(batch_size, num_atoms)
        assert torch.isfinite(log_prob_prior).all(), "NaN/inf detected in prior eval."

        # Compute unnormalized proposal posterior.
        unnormalized_log_prob_proposal_posterior = log_prob_posterior - log_prob_prior

        # Normalize proposal posterior across discrete set of atoms.
        log_prob_proposal_posterior = self.calibration_kernel(
            context
        ) * unnormalized_log_prob_proposal_posterior[:, 0] - torch.logsumexp(
            unnormalized_log_prob_proposal_posterior, dim=-1
        )
        assert torch.isfinite(
            log_prob_proposal_posterior
        ).all(), "NaN/inf detected in proposal posterior eval."

        # todo: this implementation is not perfect: it evaluates the posterior
        # todo: at all prior samples
        if self._use_combined_loss:
            log_prob_posterior_non_atomic = self._neural_posterior.log_prob(
                inputs, context, normalize_snpe_density=False
            )
            masks = masks.reshape(-1)
            log_prob_proposal_posterior = (
                masks * log_prob_posterior_non_atomic + log_prob_proposal_posterior
            )

        return log_prob_proposal_posterior

    def _get_log_prob_proposal_MoG(self, inputs, context, masks):

        raise NotImplementedError
