"""Definition of the model and the inference setup, with helper functions."""

import numpy as np
import scipy.sparse as sp
import torch
import torch.nn as nn
from torch.distributions import constraints
import pyro
import pyro.distributions as dist
import pyro.nn
from pyro.infer import config_enumerate
from cellbender.remove_background.distributions.NegativeBinomial \
    import NegativeBinomial
from cellbender.remove_background.vae import encoder as encoder_module
from cellbender.remove_background.data.dataset import Dataset

from typing import Union, Tuple
import logging


class VariationalInferenceModel(nn.Module):
    """Class that contains the model and guide used for variational inference.

    Args:
        model_type: Which model is being used, one of ['simple', 'ambient',
            'swapping', 'contamination'].
        encoder: An instance of an encoder object.  Can be a CompositeEncoder.
        decoder: An instance of a decoder object.
        dataset: Dataset object which contains relevant priors.
        phi_loc_prior: Location parameter for the prior of a Gamma distribution
            for the overdispersion, Phi, of the negative binomial distribution
            used for sampling counts.
        phi_scale_prior: Location parameter for the prior of a Gamma
            distribution for the overdispersion, Phi, of the negative binomial
            distribution used for sampling counts.
        rho_alpha_prior: Alpha parameter for Beta distribution of the
            contamination parameter, rho.
        rho_beta_prior: Beta parameter for Beta distribution of the
            contamination parameter, rho.
        use_decaying_avg_baseline: Boolean for whether or not to use decaying
            average baselines during the inference procedure.
        lambda_reg: Scale factor for L1 regularization to be applied to the
            decoder weight matrices.
        use_cuda: Will use GPU if True.

    Attributes:
        All the above, plus
        device: Either 'cpu' or 'cuda' depending on value of use_cuda.

    """

    def __init__(self,
                 model_type: str,
                 encoder: Union[nn.Module, encoder_module.CompositeEncoder],
                 decoder: nn.Module,
                 dataset_obj: Dataset,
                 phi_loc_prior: float = 0.2,
                 phi_scale_prior: float = 0.2,
                 rho_alpha_prior: float = 3,
                 rho_beta_prior: float = 80,
                 use_decaying_avg_baseline: bool = False,
                 use_IAF: bool = False,
                 lambda_reg: float = 0.,
                 use_cuda: bool = False):
        super(VariationalInferenceModel, self).__init__()

        self.model_type = model_type
        self.include_empties = True
        if self.model_type == "simple":
            self.include_empties = False
        self.include_rho = False
        if (self.model_type == "full") or (self.model_type == "swapping"):
            self.include_rho = True

        self.use_decaying_avg_baseline = use_decaying_avg_baseline
        self.use_IAF = use_IAF
        self.n_genes = dataset_obj.analyzed_gene_inds.size
        self.z_dim = decoder.input_dim
        self.encoder = encoder
        self.decoder = decoder
        self.loss = {'train': {'epoch': [], 'elbo': []},
                     'test': {'epoch': [], 'elbo': []}}

        # Inverse autoregressive flow
        if self.use_IAF:
            num_iafs = 1
            iaf_dim = self.z_dim
            iafs = [dist.iaf.InverseAutoregressiveFlow(
                pyro.nn.AutoRegressiveNN(self.z_dim, [iaf_dim]))
                for _ in range(num_iafs)]
            self.iafs = iafs  # pyro's recommended 'nn.ModuleList(iafs)' is wrong
            if len(self.iafs) > 0:
                logging.info("Using inverse autoregressive flows for inference.")
        else:
            self.iafs = []

        # Determine whether we are working on a GPU.
        if use_cuda:
            # Calling cuda() here will put all the parameters of
            # the encoder and decoder networks into GPU memory.
            self.cuda()
            try:
                for key, value in self.encoder.items():
                    value.cuda()
            except KeyError:
                pass
            if len(self.iafs) > 0:
                for iaf in self.iafs:
                    iaf.cuda()
            self.device = 'cuda'
        else:
            self.device = 'cpu'
        self.use_cuda = use_cuda

        # Priors
        assert dataset_obj.priors['d_std'] > 0, f"Issue with prior: d_std is " \
                                                f"{dataset_obj.priors['d_std']}, " \
                                                f"but should be > 0."
        assert dataset_obj.priors['cell_counts'] > 0, \
            f"Issue with prior: cell_counts is " \
            f"{dataset_obj.priors['cell_counts']}, but should be > 0."

        self.d_cell_loc_prior = (np.log1p(dataset_obj.priors['cell_counts'],
                                          dtype=np.float32).item()
                                 * torch.ones(torch.Size([])).to(self.device))
        self.d_cell_scale_prior = (np.array(dataset_obj.priors['d_std'],
                                            dtype=np.float32).item()
                                   * torch.ones(torch.Size([])).to(self.device))
        self.z_loc_prior = torch.zeros(torch.Size([self.z_dim])).to(self.device)
        self.z_scale_prior = torch.ones(torch.Size([self.z_dim])).to(self.device)
        self.lambda_reg = lambda_reg

        if self.model_type != "simple":

            assert dataset_obj.priors['empty_counts'] > 0, \
                f"Issue with prior: empty_counts should be > 0, but is " \
                f"{dataset_obj.priors['empty_counts']}"
            chi_ambient_sum = np.round(dataset_obj.priors['chi_ambient'].sum().item(),
                                       decimals=4).item()
            assert chi_ambient_sum == 1., f"Issue with prior: chi_ambient should " \
                                          f"sum to 1, but is {chi_ambient_sum}"
            chi_bar_sum = np.round(dataset_obj.priors['chi_bar'].sum().item(),
                                   decimals=4)
            assert chi_bar_sum == 1., f"Issue with prior: chi_bar should " \
                                      f"sum to 1, but is {chi_bar_sum}"

            self.d_empty_loc_prior = (np.log1p(dataset_obj.priors['empty_counts'],
                                               dtype=np.float32).item()
                                      * torch.ones(torch.Size([])).to(self.device))
            self.d_empty_scale_prior = (np.array(dataset_obj.priors['d_std'],
                                                 dtype=np.float32).item()
                                        * torch.ones(torch.Size([])).to(self.device))

            self.p_logit_prior = (dataset_obj.priors['cell_logit']
                                  * torch.ones(torch.Size([])).to(self.device))

            self.chi_ambient_init = dataset_obj.priors['chi_ambient'].to(self.device)
            self.avg_gene_expression = dataset_obj.priors['chi_bar'].to(self.device)

        else:

            self.avg_gene_expression = None

        self.phi_loc_prior = (phi_loc_prior
                              * torch.ones(torch.Size([])).to(self.device))
        self.phi_scale_prior = (phi_scale_prior
                                * torch.ones(torch.Size([])).to(self.device))
        self.phi_conc_prior = ((phi_loc_prior ** 2 / phi_scale_prior ** 2)
                               * torch.ones(torch.Size([])).to(self.device))
        self.phi_rate_prior = ((phi_loc_prior / phi_scale_prior ** 2)
                               * torch.ones(torch.Size([])).to(self.device))

        self.rho_alpha_prior = (rho_alpha_prior
                                * torch.ones(torch.Size([])).to(self.device))
        self.rho_beta_prior = (rho_beta_prior
                               * torch.ones(torch.Size([])).to(self.device))

    def _calculate_mu(self,
                      chi: torch.Tensor,
                      d_cell: torch.Tensor,
                      chi_ambient: torch.Tensor,
                      d_empty: torch.Tensor,
                      y: torch.Tensor,
                      rho: torch.Tensor,
                      chi_bar: torch.Tensor):
        """Implement a calculation of mean expression based on the model."""

        if self.model_type == "simple":
            """The model is that a latent variable z is drawn from a z_dim dimensional
            normal distribution.  This latent z is put through the decoder to
            generate a full vector of fractional gene expression, chi.  Counts are
            then drawn from a negative binomial distribution with mean d * chi.
            d is drawn from a LogNormal distribution with the specified prior.
            Phi is the overdispersion of this negative binomial, and is drawn
            from a Gamma distribution with the specified prior.

            """
            mu = d_cell.unsqueeze(-1) * chi

        elif self.model_type == "ambient":
            """There is a global hyperparameter called chi_ambient.  This parameter
            is the learned fractional gene expression vector for ambient RNA.
            The model is that a latent variable z is drawn from a z_dim dimensional
            normal distribution.  This latent z is put through the decoder to
            generate a full vector of fractional gene expression, chi.  Counts
            are then drawn from a negative binomial distribution with mean
            d_cell * chi + d_ambient * chi_ambient.  d_cell is drawn from a
            LogNormal distribution with the specified prior, as is d_ambient.
            Phi is the overdispersion of this negative binomial, and is
            drawn from a Gamma distribution with the specified prior.

            """
            mu = (y.unsqueeze(-1) * d_cell.unsqueeze(-1) * chi
                  + d_empty.unsqueeze(-1) * chi_ambient.unsqueeze(0))

        elif self.model_type == "full":
            """There is a global hyperparameter called chi_ambient.  This parameter
            is the learned fractional gene expression vector for ambient RNA, which
            in this model could be a combination of cell-free RNA and the average
            of cellular RNA which has been erroneously barcode-swapped.
            The model is that a latent variable z is drawn from a z_dim dimensional
            normal distribution.  This latent z is put through the decoder to
            generate a full vector of fractional gene expression, chi.  Counts
            are then drawn from a negative binomial distribution with mean
            (1 - rho) * [y * d * chi + d_ambient * chi_ambient]
            + rho * (y * d + d_ambient) * chi_average.  d is drawn from a
            LogNormal distribution with the specified prior.  Phi is the
            overdispersion of this negative binomial, and is drawn from a Gamma
            distribution with the specified prior.  Rho is the contamination
            fraction, or swapping / stealing fraction, i.e. the fraction of reads in
            the cell barcode that do not originate from that cell barcode's droplet.
    
            """
            mu = ((1 - rho.unsqueeze(-1))
                  * (y.unsqueeze(-1) * d_cell.unsqueeze(-1) * chi
                     + d_empty.unsqueeze(-1) * chi_ambient.unsqueeze(0))
                  + rho.unsqueeze(-1)
                  * (y.unsqueeze(-1) * d_cell.unsqueeze(-1)
                     + d_empty.unsqueeze(-1))
                  * chi_bar)

        elif self.model_type == "swapping":
            """The parameter chi_average is the average of cellular RNA which has been
            erroneously barcode-swapped or otherwise mis-assigned to another barcode.
            The model is that a latent variable z is drawn from a z_dim dimensional
            normal distribution.  This latent z is put through the decoder to
            generate a full vector of fractional gene expression, chi.  Counts
            are then drawn from a negative binomial distribution with mean
            (1 - rho) * [y * d * chi] + (rho * y * d + d_ambient) * chi_average.
            d is drawn from a LogNormal distribution with the specified prior.
            Phi is the overdispersion of this negative binomial, and is drawn from
            a Gamma distribution with the specified prior.  Rho is the contamination
            fraction, or swapping / stealing fraction, i.e. the fraction of reads in
            the cell barcode that do not originate from that cell barcode's droplet.
    
            """
            mu = ((1 - rho.unsqueeze(-1))
                  * (y.unsqueeze(-1) * d_cell.unsqueeze(-1) * chi)
                  + (rho.unsqueeze(-1)
                     * y.unsqueeze(-1) * d_cell.unsqueeze(-1)
                     + d_empty.unsqueeze(-1)) * chi_bar)

        else:
            raise NotImplementedError(f"model_type was set to {model_type}, "
                                      f"which is not implemented.")

        return mu

    def model(self, x, observe=True) -> torch.Tensor:
        """Data likelihood model."""

        # Register the decoder with pyro.
        pyro.module("decoder", self.decoder)

        # Register the hyperparameter for ambient gene expression.
        if self.include_empties:
            chi_ambient = pyro.param("chi_ambient",
                                     self.chi_ambient_init *
                                     torch.ones(torch.Size([])).to(self.device),
                                     constraint=constraints.simplex)
        else:
            # chi_ambient = torch.rand(1)  # dummy tensor for Jit static typing
            chi_ambient = None

        # Sample phi from Gamma prior.
        phi = pyro.sample("phi",
                          dist.Gamma(self.phi_conc_prior,
                                     self.phi_rate_prior))

        # Add L1 regularization term to the loss based on decoder weights.
        # self._regularize(x.size(0))

        # Happens in parallel for each data point (cell barcode) independently:
        with pyro.plate("data", x.size(0),
                        use_cuda=self.use_cuda, device=self.device):

            # Sample z from prior.
            z = pyro.sample("z",
                            dist.Normal(self.z_loc_prior,
                                        self.z_scale_prior)
                            .expand_by([x.size(0)]).to_event(1))

            # Decode the latent code z to get fractional gene expression, chi.
            chi = self.decoder.forward(z)

            # For data generation only, so that we can condition the model on chi.
            if not observe:
                chi = pyro.sample("chi", dist.Delta(chi).to_event(1))

            # Sample d_cell based on priors.
            d_cell = pyro.sample("d_cell",
                                 dist.LogNormal(self.d_cell_loc_prior,
                                                self.d_cell_scale_prior)
                                 .expand_by([x.size(0)]))

            # Sample swapping fraction rho.
            if self.include_rho:
                rho = pyro.sample("rho", dist.Beta(self.rho_alpha_prior,
                                                   self.rho_beta_prior)
                                  .expand_by([x.size(0)]))
            else:
                # rho = torch.rand(1)  # dummy tensor for Jit static typing
                rho = None

            # If modelling empty droplets:
            if self.include_empties:

                # Sample d_empty based on priors.
                d_empty = pyro.sample("d_empty",
                                      dist.LogNormal(self.d_empty_loc_prior,
                                                     self.d_empty_scale_prior)
                                      .expand_by([x.size(0)]))

                # Sample y, denoting presence of a real cell, based on p_logit_prior.
                y = pyro.sample("y",
                                dist.Bernoulli(logits=self.p_logit_prior)
                                .expand_by([x.size(0)]))

            else:
                d_empty = torch.rand(1)  # dummy tensor for Jit static typing
                d_empty = None
                # y = torch.rand(1)  # dummy tensor for Jit static typing
                y = None

            # Calculate the mean gene expression counts (for each barcode).
            mu = self._calculate_mu(chi, d_cell,
                                    chi_ambient=chi_ambient,
                                    d_empty=d_empty,
                                    y=y,
                                    rho=rho,
                                    chi_bar=self.avg_gene_expression)

            # Sample actual gene expression, and compare with observed data.
            r = 1. / phi
            logit = torch.log(mu * phi)

            if observe:
                # Poisson:
                # pyro.sample("obs", dist.Poisson(mu).independent(1),
                #             obs=x.reshape(-1, self.n_genes))

                # Negative binomial:
                c = pyro.sample("obs", NegativeBinomial(total_count=r,
                                                        logits=logit).to_event(1),
                                obs=x.reshape(-1, self.n_genes))
            else:
                # For data generation only
                c = pyro.sample("obs", NegativeBinomial(total_count=r,
                                                        logits=logit).to_event(1))

        return c

    @config_enumerate(default='parallel')
    def guide(self, x, observe=True):
        """Variational posterior."""

        # Register the encoder(s) with pyro.
        for name, module in self.encoder.items():
            pyro.module("encoder_" + name, module)

        # If necessary, register the IAF with pyro.
        for i, iaf in enumerate(self.iafs):
            pyro.module(f"iaf_{i}", iaf)

        # Initialize variational parameters for d_cell.
        d_cell_scale = pyro.param("d_cell_scale",
                                  self.d_cell_scale_prior *
                                  torch.ones(torch.Size([])).to(self.device),
                                  constraint=constraints.positive)

        if self.include_empties:

            # Initialize variational parameters for d_empty.
            d_empty_loc = pyro.param("d_empty_loc",
                                     self.d_empty_loc_prior *
                                     torch.ones(torch.Size([])).to(self.device),
                                     constraint=constraints.positive)
            d_empty_scale = pyro.param("d_empty_scale",
                                       self.d_empty_scale_prior *
                                       torch.ones(torch.Size([])).to(self.device),
                                       constraint=constraints.positive)

            # Register the hyperparameter for ambient gene expression.
            chi_ambient = pyro.param("chi_ambient",
                                     self.chi_ambient_init *
                                     torch.ones(torch.Size([])).to(self.device),
                                     constraint=constraints.simplex)

        # Initialize variational parameters for rho.
        if self.include_rho:
            rho_alpha = pyro.param("rho_alpha",
                                   self.rho_alpha_prior *
                                   torch.ones(torch.Size([])).to(self.device),
                                   constraint=constraints.positive)
            rho_beta = pyro.param("rho_beta",
                                  self.rho_beta_prior *
                                  torch.ones(torch.Size([])).to(self.device),
                                  constraint=constraints.positive)

        # Initialize variational parameters for phi.
        phi_loc = pyro.param("phi_loc",
                             self.phi_loc_prior *
                             torch.ones(torch.Size([])).to(self.device),
                             constraint=constraints.positive)
        phi_scale = pyro.param("phi_scale",
                               self.phi_scale_prior *
                               torch.ones(torch.Size([])).to(self.device),
                               constraint=constraints.positive)

        # Decaying average baselines.
        baseline_dict = {'use_decaying_avg_baseline': self.use_decaying_avg_baseline,
                         'baseline_beta': 0.9}

        # Sample phi from a Gamma distribution (after re-parameterization).
        phi_conc = phi_loc.pow(2) / phi_scale.pow(2)
        phi_rate = phi_loc / phi_scale.pow(2)
        pyro.sample("phi", dist.Gamma(phi_conc, phi_rate))

        # Happens in parallel for each data point (cell barcode) independently:
        with pyro.plate("data", x.size(0),
                        use_cuda=self.use_cuda, device=self.device):

            # Encode the latent variables from the input gene expression counts.
            if self.include_empties:
                enc = self.encoder.forward(x, chi_ambient)

            else:
                enc = self.encoder.forward(x, None)

            # Sample swapping fraction rho.
            if self.include_rho:
                pyro.sample("rho", dist.Beta(rho_alpha,
                                             rho_beta).expand_by([x.size(0)]))

            # Code specific to models with empty droplets.
            if self.include_empties:

                # Sample d_empty, which doesn't depend on y.
                pyro.sample("d_empty",
                            dist.LogNormal(d_empty_loc,
                                           d_empty_scale).expand_by([x.size(0)]))

                # Mask out the barcodes which are likely to be empty droplets.
                # masking = (enc['p_y'] >= 0).to(self.device, dtype=torch.float32)
                masking = dist.Bernoulli(logits=enc['p_y']).sample()  # for Jit

                # Determine the posterior distribution for z...
                z_dist = dist.Normal(enc['z']['loc'], enc['z']['scale'])

                # ... adding an inverse autoregressive flow if called for.
                if len(self.iafs) > 0:
                    z_dist = dist.TransformedDistribution(z_dist, self.iafs)

                # Sample latent code z for the barcodes containing real cells.
                pyro.sample("z", z_dist.to_event(1).mask(masking))

                # Sample the Bernoulli y from encoded p(y).
                pyro.sample("y", dist.Bernoulli(logits=enc['p_y']),
                            infer=dict(baseline=baseline_dict))

                # Gate d_cell_loc so empty droplets do not give big gradients.
                prob = enc['p_y'].sigmoid()  # Logits to probability
                d_cell_loc_gated = (prob * enc['d_loc'] + (1 - prob)
                                    * self.d_cell_loc_prior)

                # Sample d based the encoding.
                pyro.sample("d_cell", dist.LogNormal(d_cell_loc_gated, d_cell_scale))

            else:

                # Sample d based the encoding.
                pyro.sample("d_cell", dist.LogNormal(enc['d_loc'], d_cell_scale))

                # Sample latent code z for each cell.
                pyro.sample("z", dist.Normal(enc['z']['loc'],
                                             enc['z']['scale']).independent(1))

    def _regularize(self, n_batch: int):
        """Helper function to add an L1 regularization term based on decoder.

        Args:
            n_batch: Size of the mini-batch of data.

        Note: This is not currently used.

        """

        assert self.lambda_reg >= 0, f"Regularization parameter lambda_reg must " \
                                     f"be > 0, but was found to be {self.lambda_reg}"

        # Only if the regularization parameter lambda is greater than zero.
        if self.lambda_reg > 0.:

            penalty = 0
            n = 0

            # Go through all the weights in each decoder hidden layer.
            for lin in self.decoder.linears:

                # Add up the L1 norm.
                penalty += lin.weight.abs().sum()

                # Count the number of weights being added.
                n = n + lin.in_features * lin.out_features

            # Add the weights in the output decoder layer.
            penalty += self.decoder.outlinear.weight.abs().sum()

            # Count the number of weights.
            n += (self.decoder.outlinear.in_features
                  * self.decoder.outlinear.out_features)

            # Normalize the penalty by the number of weights and the minibatch
            # size, to keep it independent of minibatch size.
            penalty = penalty / n * self.lambda_reg * n_batch

            # Add the penalty term to the ELBO.
            self._add_loss(name="decoder_L1_loss",
                           loss=penalty,
                           model_or_guide="model")

    def _add_loss(self, name: str, loss: torch.Tensor, model_or_guide: str):
        """Add a loss term to the ELBO, using the Bernoulli trick.

        Note: The following is assumed to be correct.

        The log prob of a Bernoulli with an observation of zero
        where logits<0 is just logits.
        The log prob of a Bernoulli with an observation of one
        where logits>0 is just -logits.

        TODO: check the above assumptions.

        Args:
            name: The name for the pyro sample that will be created.
            loss: The loss value to be added to the ELBO.  This should be a
                single float wrapped in a torch.Tensor.
            model_or_guide: Specifies whether this call was from the model or
                from the guide.  Must be one of ['model', 'guide'].

        """

        if model_or_guide == 'model':

            # Ensure loss is negative.
            if loss > 0:
                loss = -1 * loss

            # Sample a dummy Bernoulli, observation = 0, to add the loss term.
            pyro.sample(name, dist.Bernoulli(logits=loss),
                        obs=torch.zeros_like(loss))

        elif model_or_guide == 'guide':

            # Ensure loss is positive.
            if loss < 0:
                loss = -1 * loss

            # Sample a dummy Bernoulli, observation = 1, to add the loss term.
            pyro.sample(name, dist.Bernoulli(logits=loss),
                        obs=torch.ones_like(loss),
                        infer={'is_auxiliary': True})

        else:

            raise Exception(f"model_or_guide must be one of ['model', 'guide'], "
                            f"but was {model_or_guide}")

    def save_model_to_file(self, file_name: str):
        """Save current state of the model to disk.

        TODO: This is incomplete.

            This saves the param store dict, but I don't think this is enough
            to actually pick up and start training again.
            What about the torch module objects, like encoder, model, and
            the the SVI optimizer?  And the dataloaders...?

        Note:
            May not be necessary to worry about SVI, but need to save the
            attributes of the model object, and the encoder and decoder objects.

            Or maybe all you need to save is the input args... and then just
            go through the same process you did the first time.

            All you need is to have the same objects with the same names and
            same attributes.  Then the param store can fill in values.

        """

        raise NotImplementedError("Trying to save model to file, but this"
                                  "is not yet implemented.")

        # torch.save(self, '.'.join([file_name, 'torch']))
        # pyro.get_param_store().save('.'.join([file_name, 'pyro']))

    def load_model_from_file(self, file_name: str):
        """Load a model state from disk.

        TODO: This is incomplete.

        """

        raise NotImplementedError("Trying to load model from file, but this"
                                  "is not yet implemented.")

        # # Load model via torch.
        #
        #
        # # Load pyro param store.
        # pyro.clear_param_store()
        # pyro.get_param_store().load(file_name)


def get_encodings(model: VariationalInferenceModel,
                  dataset_obj,
                  cells_only: bool = True) -> Tuple[np.ndarray,
                                                    np.ndarray,
                                                    np.ndarray]:
    """Get inferred quantities from a trained model.

    Run a dataset through the model's trained encoder and return the inferred
    quantities.

    Args:
        model: A trained cellbender.model.VariationalInferenceModel, which will be
            used to generate the encodings from data.
        dataset_obj: The dataset to be encoded.
        cells_only: If True, only returns the encodings of barcodes that are
            determined to contain cells.

    Returns:
        z: Latent variable embedding of gene expression in a low-dimensional
            space.
        d: Latent variable scale factor for the number of UMI counts coming
            from each real cell.  Not in log space, but actual size.  This is
            not just the encoded d, but the mean of the LogNormal distribution,
            which is exp(mean + sigma^2 / 2).
        p: Latent variable denoting probability that each barcode contains a
            real cell.

    """

    logging.info("Encoding data according to model.")

    # Get the count matrix with genes trimmed.
    if cells_only:
        dataset = dataset_obj.get_count_matrix()
    else:
        dataset = dataset_obj.get_count_matrix_all_barcodes()

    # Initialize numpy arrays as placeholders.
    z = np.zeros((dataset.shape[0], model.z_dim))
    d = np.zeros((dataset.shape[0]))
    p = np.zeros((dataset.shape[0]))

    # Get chi ambient, if it was part of the model.
    chi_ambient = get_ambient_expression()
    if chi_ambient is not None:
        chi_ambient = torch.Tensor(chi_ambient).to(device=model.device)

    # Send dataset through the learned encoder in chunks.
    s = 200
    for i in np.arange(0, dataset.shape[0], s):

        # Put chunk of data into a torch.Tensor.
        x = torch.Tensor(np.array(
            dataset[i:min(dataset.shape[0], i + s), :].todense(),
            dtype=int).squeeze()).to(device=model.device)

        # Send data chunk through encoder.
        enc = model.encoder.forward(x, chi_ambient)

        # Get d_cell_scale from fit model.
        d_sig = \
            pyro.get_param_store().get_param('d_cell_scale').detach().cpu().numpy()

        # Put the resulting encodings into the appropriate numpy arrays.
        z[i:min(dataset.shape[0], i + s), :] = \
            enc['z']['loc'].detach().cpu().numpy()
        d[i:min(dataset.shape[0], i + s)] = \
            np.exp(enc['d_loc'].detach().cpu().numpy() + d_sig.item()**2 / 2)
        try:  # p is not always available: it depends which model was used.
            p[i:min(dataset.shape[0], i + s)] = \
                enc['p_y'].detach().sigmoid().cpu().numpy()
        except KeyError:
            p = None  # Simple model gets None for p.

    return z, d, p


def get_count_matrix_from_encodings(z: np.ndarray,
                                    d: np.ndarray,
                                    p: Union[np.ndarray, None],
                                    model: VariationalInferenceModel,
                                    dataset_obj,
                                    cells_only: bool = True) -> sp.csc.csc_matrix:
    """Make point estimate of the ambient-background-subtracted UMI count matrix.

    Sample counts by maximizing the model posterior based on learned latent
    variables.  The output matrix is in sparse form.

    Args:
        z: Latent variable embedding of gene expression in a low-dimensional
            space.
        d: Latent variable scale factor for the number of UMI counts coming
            from each real cell.
        p: Latent variable denoting probability that each barcode contains a
            real cell.
        model: Model with latent variables already inferred.
        dataset_obj: Input dataset.
        cells_only: If True, only returns the encodings of barcodes that are
            determined to contain cells.

    Returns:
        inferred_count_matrix: Matrix of the same dimensions as the input
            matrix, but where the UMI counts have had ambient-background
            subtracted.

    Note:
        This currently uses the MAP estimate of draws from a Poisson (or a
        negative binomial with zero overdispersion).

    """

    # If simple model was used, then p = None.  Here set it to 1.
    if p is None:
        p = np.ones_like(d)

    # Get the count matrix with genes trimmed.
    if cells_only:
        count_matrix = dataset_obj.get_count_matrix()
    else:
        count_matrix = dataset_obj.get_count_matrix_all_barcodes()

    logging.info("Getting ambient-background-subtracted UMI count matrix.")

    # Ensure there are no nans in p (there shouldn't be).
    p_no_nans = p
    p_no_nans[np.isnan(p)] = 0  # Just make sure there are no nans.

    # Trim everything down to the barcodes we are interested in (just cells?).
    if cells_only:
        d = d[p_no_nans > 0.5]
        z = z[p_no_nans > 0.5, :]
        barcode_inds = dataset_obj.analyzed_barcode_inds[p_no_nans > 0.5]
    else:
        # Set cell size factors equal to zero where cell probability < 0.5.
        d[p_no_nans < 0.5] = 0.
        z[p_no_nans < 0.5, :] = 0.
        barcode_inds = np.arange(0, count_matrix.shape[0])  # All barcodes

    # Get mean of the inferred posterior for the overdispersion, phi.
    phi = pyro.get_param_store().get_param("phi_loc").detach().cpu().numpy().item()

    # Get the gene expression vectors by sending latent z through the decoder.
    # Send dataset through the learned encoder in chunks.
    barcodes = []
    genes = []
    counts = []
    s = 200
    for i in np.arange(0, barcode_inds.size, s):

        # TODO: for 117000 cells, this routine overflows (~15GB) memory

        last_ind_this_chunk = min(count_matrix.shape[0], i+s)

        # Decode gene expression for a chunk of barcodes.
        decoded = model.decoder(torch.Tensor(
            z[i:last_ind_this_chunk]).to(device=model.device))
        chi = decoded.detach().cpu().numpy()

        # Estimate counts for the chunk of barcodes.
        chunk_dense_counts = estimate_counts(chi,
                                             d[i:last_ind_this_chunk],
                                             phi)

        # Turn the floating point count estimates into integers.
        decimal_values, _ = np.modf(chunk_dense_counts)  # Stuff after decimal.
        roundoff_counts = np.random.binomial(1, p=decimal_values)  # Bernoulli.
        chunk_dense_counts = np.floor(chunk_dense_counts).astype(dtype=int)
        chunk_dense_counts += roundoff_counts

        # Find all the nonzero counts in this dense matrix chunk.
        nonzero_barcode_inds_this_chunk, nonzero_genes_trimmed = \
            np.nonzero(chunk_dense_counts)
        nonzero_counts = \
            chunk_dense_counts[nonzero_barcode_inds_this_chunk,
                               nonzero_genes_trimmed].flatten(order='C')

        # Get the original gene index from gene index in the trimmed dataset.
        nonzero_genes = dataset_obj.analyzed_gene_inds[nonzero_genes_trimmed]

        # Get the actual barcode values.
        nonzero_barcode_inds = nonzero_barcode_inds_this_chunk + i
        nonzero_barcodes = barcode_inds[nonzero_barcode_inds]

        # Append these to their lists.
        barcodes.extend(nonzero_barcodes.astype(dtype=np.uint32))
        genes.extend(nonzero_genes.astype(dtype=np.uint16))
        counts.extend(nonzero_counts.astype(dtype=np.uint32))

    # Convert the lists to numpy arrays.
    counts = np.array(counts, dtype=np.uint32)
    barcodes = np.array(barcodes, dtype=np.uint32)
    genes = np.array(genes, dtype=np.uint16)

    # Put the counts into a sparse csc_matrix.
    inferred_count_matrix = sp.csc_matrix((counts, (barcodes, genes)),
                                          shape=dataset_obj.data['matrix'].shape)

    return inferred_count_matrix


def estimate_counts(chi: np.ndarray,
                        d: np.ndarray,
                        phi: float) -> np.ndarray:
    """Return an estimate of the number of counts, based on inferred latents.

    Args:
        chi: Vector (or matrix) of inferred fractional gene expression, where
            rightmost dimension corresponds to genes.
        d: Vector of size scale factors, one for each barcode.  Must contain
            zeros for barcodes determined not to contain a real cell.
        phi: Overdispersion parameter of the negative binomial distribution.

    Note:
        mode of NB is p(r-1)/(1-p)
        where r = 1/phi
        and p/(1-p) = mu*phi
        so the mode is mu*phi*(1/phi-1) = mu - mu*phi = mu*(1-phi)
        and mu = chi*d
        [if phi>1, then the MAP is zero]

        But, the output is mu = chi * d, as a float.

    """

    # assert phi >= 0., "Over-dispersion of negative binomial must be >= 0."

    mu = np.expand_dims(d, axis=1) * chi
    return np.maximum(0, mu)

    # return np.maximum(0, np.array(mu * (1 - phi), dtype=int))


def get_ambient_expression() -> Union[np.ndarray, None]:
    """Get ambient RNA expression for 'empty' droplets.

    Return:
        chi_ambient: The ambient gene expression profile, as a normalized
            vector that sums to one.

    Note:
        Inference must have been performed on a model with a 'chi_ambient'
        hyperparameter prior to making this call.

    """

    chi_ambient = None

    try:
        # Get fit hyperparameter for ambient gene expression from model.
        chi_ambient = pyro.get_param_store().get_param("chi_ambient").\
            detach().cpu().numpy().squeeze()
    except Exception:
        pass

    return chi_ambient


def get_contamination_fraction() -> Union[np.ndarray, None]:
    """Get barcode swapping contamination fraction hyperparameters.

    Return:
        rho: The alpha and beta parameters of the Beta distribution for the
            contamination fraction.

    Note:
        Inference must have been performed on a model with 'rho_alpha' and
        'rho_beta' hyperparameters prior to making this call.

    """

    rho = None

    try:
        # Get fit hyperparameters for contamination fraction from model.
        rho_alpha = pyro.get_param_store().get_param("rho_alpha").\
            detach().cpu().numpy().squeeze()
        rho_beta = pyro.get_param_store().get_param("rho_beta"). \
            detach().cpu().numpy().squeeze()
        rho = np.array([rho_alpha, rho_beta])
    except Exception:
        pass

    return rho


def get_overdispersion() -> Union[np.ndarray, None]:
    """Get overdispersion hyperparameters.

    Return:
        phi: The mean and stdev parameters of the Gamma distribution for the
            contamination fraction.

    Note:
        Inference must have been performed on a model with 'phi_loc' and
        'phi_scale' hyperparameters prior to making this call.

    """

    phi = None

    try:
        # Get fit hyperparameters for contamination fraction from model.
        phi_loc = pyro.get_param_store().get_param("phi_loc").\
            detach().cpu().numpy().squeeze()
        phi_scale = pyro.get_param_store().get_param("phi_scale"). \
            detach().cpu().numpy().squeeze()
        phi = np.array([phi_loc, phi_scale])
    except Exception:
        pass

    return phi
