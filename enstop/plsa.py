import numpy as np
import numba

from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.utils import check_array
from sklearn.utils.extmath import randomized_svd
from sklearn.decomposition import non_negative_factorization
from scipy.sparse import issparse, csr_matrix, coo_matrix

from enstop.utils import (
    normalize,
    coherence,
    mean_coherence,
    log_lift,
    mean_log_lift,
)

@numba.njit(fastmath=True, nogil=True)
def plsa_e_step(
    X_rows,
    X_cols,
    X_vals,
    p_w_given_z,
    p_z_given_d,
    p_z_given_wd,
    probability_threshold=1e-32,
):
    """Perform the E-step of pLSA optimization. This
    amounts to computing the probability of each topic
    given each word document pair. The computation
    implements

    P(z|w,d) = \frac{P(z|w)P(d|z)}{\sum_{z=1}^k P(z|w)P(d|z)}.

    This routine is optimized to work with sparse matrices
    such that P(z|w,d) is only computed for w, d such that
    X_{w,d} is non-zero, where X is the data matrix.

    To make this numba compilable the raw arrays defining
    the COO format sparse matrix must be passed separately.


    Parameters
    ----------
    X_rows: array of shape (nnz,)
        For each non-zero entry of X, the row of the entry.

    X_cols: array of shape (nnz,)
        For each non-zero entry of X, the column of the
        entry.

    X_vals: array of shape (nnz,)
        For each non-zero entry of X, the value of entry.

    p_w_given_z: array of shape (n_topics, n_words)
        The current estimates of values for P(w|z)

    p_z_given_d: array of shape (n_docs, n_topics)
        The current estimates of values for P(z|d)

    p_z_given_wd: array of shape (nnz, n_topics)
        The result array to write new estimates of P(z|w,d) to.

    probability_threshold: float (optional, default=1e-32)
        Option to promote sparsity. If the value of
        P(w|z)P(z|d) falls below threshold then write a
        zero for P(z|w,d).

    """

    k = p_w_given_z.shape[0]

    for nz_idx in range(X_vals.shape[0]):
        d = X_rows[nz_idx]
        w = X_cols[nz_idx]

        norm = 0.0
        for z in range(k):
            v = p_w_given_z[z, w] * p_z_given_d[d, z]
            if v > probability_threshold:
                p_z_given_wd[nz_idx, z] = v
                norm += p_z_given_wd[nz_idx, z]
            else:
                p_z_given_wd[nz_idx, z] = 0.0
        for z in range(k):
            if norm > 0:
                p_z_given_wd[nz_idx, z] /= norm

    return p_z_given_wd


@numba.njit(fastmath=True, nogil=True)
def plsa_m_step(
    X_rows,
    X_cols,
    X_vals,
    p_w_given_z,
    p_z_given_d,
    p_z_given_wd,
    norm_pwz,
    norm_pdz,
):
    """Perform the M-step of pLSA optimization. This
    amounts to using the estimates of P(z|w,d) to
    estimate the values P(w|z) and P(z|d). The
    computation implements

    P(w|z) = \frac{\sum_{d\in D} X_{w,d}P(z|w,d)}{\sum_{d,z} X_{w,d}P(z|w,d)}
    P(z|d) = \frac{\sum_{w\in V} X_{w,d}P(z|w,d)}{\sum_{w,d} X_{w,d}P(z|w,d)}

    This routine is optimized to work with sparse matrices
    such that P(z|w,d) is only computed for w, d such that
    X_{w,d} is non-zero, where X is the data matrix.

    To make this numba compilable the raw arrays defining
    the COO format sparse matrix must be passed separately.

    Parameters
    ----------
    X_rows: array of shape (nnz,)
        For each non-zero entry of X, the row of the entry.

    X_cols: array of shape (nnz,)
        For each non-zero entry of X, the column of the
        entry.

    X_vals: array of shape (nnz,)
        For each non-zero entry of X, the value of entry.

    p_w_given_z: array of shape (n_topics, n_words)
        The result array to write new estimates of P(w|z) to.

    p_z_given_d: array of shape (n_docs, n_topics)
        The result array to write new estimates of P(z|d) to.

    p_z_given_wd: array of shape (nnz, n_topics)
        The current estimates for P(z|w,d)

    norm_pwz: array of shape (n_topics,)
        Auxilliary array used for storing row norms; this
        is passed in to save reallocations.

    norm_pdz: array of shape (n_docs,)
        Auxilliary array used for storing row norms; this
        is passed in to save reallocations.

    """

    k = p_z_given_wd.shape[1]
    n = p_z_given_d.shape[0]
    m = p_w_given_z.shape[1]

    p_w_given_z[:] = 0.0
    p_z_given_d[:] = 0.0

    norm_pwz[:] = 0.0
    norm_pdz[:] = 0.0

    for nz_idx in range(X_vals.shape[0]):
        d = X_rows[nz_idx]
        w = X_cols[nz_idx]
        x = X_vals[nz_idx]

        for z in range(k):
            s = x * p_z_given_wd[nz_idx, z]

            p_w_given_z[z, w] += s
            p_z_given_d[d, z] += s

            norm_pwz[z] += s
            norm_pdz[d] += s

    for z in range(k):
        for w in range(m):
            if norm_pwz[z] > 0:
                p_w_given_z[z, w] /= norm_pwz[z]
        for d in range(n):
            if norm_pdz[d] > 0:
                p_z_given_d[d, z] /= norm_pdz[d]

    return p_w_given_z, p_z_given_d


@numba.njit(fastmath=True, nogil=True)
def log_likelihood(X_rows, X_cols, X_vals, p_w_given_z, p_z_given_d):
    """Compute the log-likelihood of observing the data X given
    estimates for P(w|z) and P(z|d). The likelihood of X_{w,d} under
    the model is given by X_{w,d} P(w|d) = X_{w,d} P(w|z) P(z|d).
    This function returns

    \log\left(\prod_{w,d} X_{w,d} P(w|d)\right)

    This routine is optimized to work with sparse matrices
    and only compute values for w, d such that
    X_{w,d} is non-zero.

    To make this numba compilable the raw arrays defining
    the COO format sparse matrix must be passed separately.

    Parameters
    ----------
    X_rows: array of shape (nnz,)
        For each non-zero entry of X, the row of the entry.

    X_cols: array of shape (nnz,)
        For each non-zero entry of X, the column of the
        entry.

    X_vals: array of shape (nnz,)
        For each non-zero entry of X, the value of entry.

    p_w_given_z: array of shape (n_topics, n_words)
        The current estimates of values for P(w|z)

    p_z_given_d: array of shape (n_docs, n_topics)
        The current estimates of values for P(z|d)

    Returns
    -------

    log_likelihood: float
        The log of the likelihood of observing X under the
        model given by the P(z|d) and P(z|w).

    """

    result = 0.0
    k = p_w_given_z.shape[0]

    for nz_idx in range(X_vals.shape[0]):
        d = X_rows[nz_idx]
        w = X_cols[nz_idx]
        x = X_vals[nz_idx]

        p_w_given_d = 0.0
        for z in range(k):
            p_w_given_d += p_w_given_z[z, w] * p_z_given_d[d, z]

        result += x * np.log(p_w_given_d)

    return result


@numba.njit(fastmath=True, nogil=True)
def norm(x):
    """Numba compilable routine for computing the l2-norm
    of a given vector x.

    Parameters
    ----------
    x: array of shape (n,)
        The array to compute the l2-norm of.

    Returns
    -------
    n: float
        The l2-norm of the input array x.
    """
    result = 0.0

    for i in range(x.shape[0]):
        result += x[i] ** 2

    return np.sqrt(result)


@numba.jit(fastmath=True)
def plsa_init(X, k, init="random"):
    """Initialize matrices for pLSA. Specifically, given
    data X, a number of topics k, and an initialization
    method, compute matrices for P(z|d) and P(w|z) that
    can be used to begin an EM optimization of pLSA.

    Various initialization approaches are available. The
    most straightforward is "random", which randomly
    initializes values for P(z|d) and P(w|z) and
    normalizes to make them probabilities. A second
    approach, borrowing from sklearn's NMF implementation,
    is to use a non-negative SVD approach ("nndsvd"). A
    third option is the use the fast coordinate descent
    under Frobenius loss version of NMF and then
    normalize to make probabilities ("nmf"). Finally if
    the ``init`` parameter is a tuple of ndarrays then
    these will be used, allowing for custom user defined
    initializations.

    Parameters
    ----------
    X: sparse matrix of shape (n_docs, n_words)
        The data matrix pLSA is attempting to fit to.

    k: int
        The number of topics for pLSA to fit with.

    init: string or tuple (optional, default="random")
        The intialization method to use. This should be
        one of:
            * ``"random"``
            * ``"nndsvd"``
            * ``"nmf"``
        or a tuple of two ndarrays of shape
        (n_docs, n_topics) and (n_topics, n_words).

    Returns
    -------
    p_z_given_d, p_w_given_z: arrays of shapes (n_docs, n_topics) and (n_topics, n_words)
        Initialized arrays suitable to passing to
        pLSA optimization methods.
    """

    n = X.shape[0]
    m = X.shape[1]

    if init == "random":
        p_w_given_z = np.random.random((k, m))
        p_z_given_d = np.random.random((n, k))

    elif init == "nndsvd":
        # Taken from sklearn NMF implementation
        U, S, V = randomized_svd(X, k)
        p_z_given_d, p_w_given_z = np.zeros(U.shape), np.zeros(V.shape)

        # The leading singular triplet is non-negative
        # so it can be used as is for initialization.
        p_z_given_d[:, 0] = np.sqrt(S[0]) * np.abs(U[:, 0])
        p_w_given_z[0, :] = np.sqrt(S[0]) * np.abs(V[0, :])

        for j in range(1, k):
            x, y = U[:, j], V[j, :]

            # extract positive and negative parts of column vectors
            x_p, y_p = np.maximum(x, 0), np.maximum(y, 0)
            x_n, y_n = np.abs(np.minimum(x, 0)), np.abs(np.minimum(y, 0))

            # and their norms
            x_p_nrm, y_p_nrm = norm(x_p), norm(y_p)
            x_n_nrm, y_n_nrm = norm(x_n), norm(y_n)

            m_p, m_n = x_p_nrm * y_p_nrm, x_n_nrm * y_n_nrm

            # choose update
            if m_p > m_n:
                u = x_p / x_p_nrm
                v = y_p / y_p_nrm
                sigma = m_p
            else:
                u = x_n / x_n_nrm
                v = y_n / y_n_nrm
                sigma = m_n

            lbd = np.sqrt(S[j] * sigma)
            p_z_given_d[:, j] = lbd * u
            p_w_given_z[j, :] = lbd * v

    elif init == "nmf":
        p_z_given_d, p_w_given_z, _ = non_negative_factorization(
            X,
            n_components=k,
            init="nndsvd",
            solver="cd",
            beta_loss=2,
            tol=1e-2,
            max_iter=100,
        )
    elif isinstance(init, tuple) or isinstance(init, list):
        p_z_given_d, p_w_given_z = init
    else:
        raise ValueError("Unrecognized init {}".format(init))

    normalize(p_w_given_z, axis=1)
    normalize(p_z_given_d, axis=1)

    return p_z_given_d, p_w_given_z


@numba.njit(fastmath=True, nogil=True)
def plsa_fit_inner(
    X_rows,
    X_cols,
    X_vals,
    p_w_given_z,
    p_z_given_d,
    n_iter=100,
    n_iter_per_test=10,
    tolerance=0.001,
    e_step_thresh=1e-32,
):
    """Internal loop of EM steps required to optimize pLSA,
    along with relative convergence tests with respect
    to the log-likelihood of observing the data under the
    model.

    The EM looping will stop when either ``n_iter`` iterations
    have been reached, or if the relative improvement in
    log-likelihood over the last ``n_iter_per_test`` steps
    is under ``threshold``.

    This function is designed to wrap the internals of the EM
    process in a numba compilable loop, and is not the preferred
    entry point for fitting a plsa model.

    Parameters
    ----------
    X_rows: array of shape (nnz,)
        For each non-zero entry of X, the row of the entry.

    X_cols: array of shape (nnz,)
        For each non-zero entry of X, the column of the
        entry.

    X_vals: array of shape (nnz,)
        For each non-zero entry of X, the value of entry.

    p_w_given_z: array of shape (n_topics, n_words)
        The current estimates of values for P(w|z)

    p_z_given_d: array of shape (n_docs, n_topics)
        The current estimates of values for P(z|d)

    n_iter: int
        The maximum number iterations of EM to perform

    n_iter_per_test: int
        The number of iterations between tests for
        relative improvement in log-likelihood.

    tolerance: float
        The threshold of relative improvement in
        log-likelihood required to continue iterations.

    e_step_thresh: float (optional, default=1e-32)
        Option to promote sparsity. If the value of
        P(w|z)P(z|d) in the E step falls below threshold
        then write a zero for P(z|w,d).

    Returns
    -------
    p_z_given_d, p_w_given_z: arrays of shapes (n_docs, n_topics) and (n_topics, n_words)
        The resulting model values of P(z|d) and P(w|z)

    """
    k = p_z_given_d.shape[1]
    n = p_z_given_d.shape[0]

    p_z_given_wd = np.zeros((X_vals.shape[0], k))

    norm_pwz = np.zeros(k)
    norm_pdz = np.zeros(n)

    previous_log_likelihood = log_likelihood(
        X_rows, X_cols, X_vals, p_w_given_z, p_z_given_d
    )

    for i in range(n_iter):

        plsa_e_step(
            X_rows,
            X_cols,
            X_vals,
            p_w_given_z,
            p_z_given_d,
            p_z_given_wd,
            e_step_thresh,
        )
        plsa_m_step(
            X_rows,
            X_cols,
            X_vals,
            p_w_given_z,
            p_z_given_d,
            p_z_given_wd,
            norm_pwz,
            norm_pdz,
        )

        if i % n_iter_per_test == 0:
            current_log_likelihood = log_likelihood(
                X_rows, X_cols, X_vals, p_w_given_z, p_z_given_d
            )
            change = np.abs(current_log_likelihood - previous_log_likelihood)
            if change / np.abs(current_log_likelihood) < tolerance:
                break
            else:
                previous_log_likelihood = current_log_likelihood

    return p_z_given_d, p_w_given_z


def plsa_fit(
    X,
    k,
    init="random",
    n_iter=100,
    n_iter_per_test=10,
    tolerance=0.001,
    e_step_thresh=1e-32,
):
    """Fit a pLSA model to a data matrix ``X`` with
    ``k`` topics, an initialized according to ``init``.
    This will run an EM method to optimize estimates
    of P(z|d) and P(w|z). The will perform at most
    ``n_iter`` EM step iterations, while checking
    for relative improvement of the log-likelihood
    of the data under the model every ``n_iter_per_test``
    iterations, and stops early if that is under
    ``tolerance``.

    Parameters
    ----------
    X: sparse matrix of shape (n_docs, n_words)
        The data matrix pLSA is attempting to fit to.

    k: int
        The number of topics for pLSA to fit with.

    init: string or tuple (optional, default="random")
        The intialization method to use. This should be
        one of:
            * ``"random"``
            * ``"nndsvd"``
            * ``"nmf"``
        or a tuple of two ndarrays of shape
        (n_docs, n_topics) and (n_topics, n_words).

    n_iter: int
        The maximum number iterations of EM to perform

    n_iter_per_test: int
        The number of iterations between tests for
        relative improvement in log-likelihood.

    tolerance: float
        The threshold of relative improvement in
        log-likelihood required to continue iterations.

    e_step_thresh: float (optional, default=1e-32)
        Option to promote sparsity. If the value of
        P(w|z)P(z|d) in the E step falls below threshold
        then write a zero for P(z|w,d).

    Returns
    -------
    p_z_given_d, p_w_given_z: arrays of shapes (n_docs, n_topics) and (n_topics, n_words)
        The resulting model values of P(z|d) and P(w|z)

    """

    p_z_given_d, p_w_given_z = plsa_init(X, k, init=init)

    A = X.tocoo()

    p_z_given_d, p_w_given_z = plsa_fit_inner(
        A.row,
        A.col,
        A.data,
        p_w_given_z,
        p_z_given_d,
        n_iter,
        n_iter_per_test,
        tolerance,
        e_step_thresh,
    )

    return p_z_given_d, p_w_given_z


@numba.njit(nogil=True)
def plsa_refit_m_step(
    X_rows, X_cols, X_vals, p_w_given_z, p_z_given_d, p_z_given_wd, norm_pdz
):
    """Optimized routine for the M step fitting values of P(z|d)
    given a fixed set of topics (i.e. P(w|z)).

    This routine is optimized to work with sparse matrices
    and only compute values for w, d such that
    X_{w,d} is non-zero.

    To make this numba compilable the raw arrays defining
    the COO format sparse matrix must be passed separately.

    Parameters
    ----------
    X_rows: array of shape (nnz,)
        For each non-zero entry of X, the row of the entry.

    X_cols: array of shape (nnz,)
        For each non-zero entry of X, the column of the
        entry.

    X_vals: array of shape (nnz,)
        For each non-zero entry of X, the value of entry.

    p_w_given_z: array of shape (n_topics, n_words)
        The fixed topics P(w|z) to fit P(z|d) against.

    p_z_given_d: array of shape (n_docs, n_topics)
        The result array to write new estimates of P(z|d) to.

    p_z_given_wd: array of shape (nnz, n_topics)
        The current estimates for P(z|w,d)

    norm_pdz: array of shape (n_docs,)
        Auxilliary array used for storing row norms; this
        is passed in to save reallocations.


    Returns
    -------

    """

    k = p_z_given_wd.shape[1]
    n = p_z_given_d.shape[0]

    p_z_given_d[:] = 0.0
    norm_pdz[:] = 0.0

    for nz_idx in range(X_vals.shape[0]):
        d = X_rows[nz_idx]
        w = X_cols[nz_idx]
        x = X_vals[nz_idx]

        for z in range(k):
            s = x * p_z_given_wd[nz_idx, z]
            p_z_given_d[d, z] += s
            norm_pdz[d] += s

    for z in range(k):
        for d in range(n):
            if norm_pdz[d] > 0:
                p_z_given_d[d, z] /= norm_pdz[d]

    return p_w_given_z, p_z_given_d


@numba.njit(nogil=True)
def plsa_refit(
    X_rows,
    X_cols,
    X_vals,
    topics,
    n_documents,
    n_iter=50,
    n_iter_per_test=5,
    tolerance=0.001,
    e_step_thresh=1e-16,
):
    """Optimized routine for refitting values of P(z|d)
    given a fixed set of topics (i.e. P(w|z)). This
    allows fitting document vectors to a predefined
    set of topics (given, for example, by an ensemble
    result).

    This routine is optimized to work with sparse matrices
    and only compute values for w, d such that
    X_{w,d} is non-zero.

    To make this numba compilable the raw arrays defining
    the COO format sparse matrix must be passed separately.

    Parameters
    ----------
    X_rows: array of shape (nnz,)
        For each non-zero entry of X, the row of the entry.

    X_cols: array of shape (nnz,)
        For each non-zero entry of X, the column of the
        entry.

    X_vals: array of shape (nnz,)
        For each non-zero entry of X, the value of entry.

    topics: array of shape (n_topics, n_words)
        The fixed topics against which to fit the values of P(z|d).

    n_documents: int
        The number of distinct documents in the corpus (rows of X).

    n_iter: int
        The maximum number iterations of EM to perform

    n_iter_per_test: int
        The number of iterations between tests for
        relative improvement in log-likelihood.

    tolerance: float
        The threshold of relative improvement in
        log-likelihood required to continue iterations.

    e_step_thresh: float (optional, default=1e-32)
        Option to promote sparsity. If the value of
        P(w|z)P(z|d) in the E step falls below threshold
        then write a zero for P(z|w,d).

    Returns
    -------
    p_z_given_d, p_w_given_z: arrays of shapes (n_docs, n_topics) and (n_topics, n_words)
        The resulting model values of P(z|d) and P(w|z)

    """

    k = topics.shape[0]

    p_z_given_d = np.random.random((n_documents, k))
    p_z_given_wd = np.zeros((X_vals.shape[0], k))

    norm_pdz = np.zeros(n_documents)

    normalize(p_z_given_d, axis=1)

    previous_log_likelihood = log_likelihood(
        X_rows, X_cols, X_vals, topics, p_z_given_d
    )

    for i in range(n_iter):

        plsa_e_step(
            X_rows, X_cols, X_vals, topics, p_z_given_d, p_z_given_wd, e_step_thresh
        )
        plsa_refit_m_step(
            X_rows, X_cols, X_vals, topics, p_z_given_d, p_z_given_wd, norm_pdz,
        )

        if i % n_iter_per_test == 0:
            current_log_likelihood = log_likelihood(
                X_rows, X_cols, X_vals, topics, p_z_given_d
            )
            if current_log_likelihood > 0:
                change = np.abs(current_log_likelihood - previous_log_likelihood)
                if change / np.abs(current_log_likelihood) < tolerance:
                    break
                else:
                    previous_log_likelihood = current_log_likelihood

    return p_z_given_d


class PLSA(BaseEstimator, TransformerMixin):
    def __init__(
        self,
        n_components=10,
        init="nndsvd",
        n_iter=100,
        n_iter_per_test=10,
        tolerance=0.001,
        e_step_thresh=1e-16,
    ):

        self.n_components = n_components
        self.init = init
        self.n_iter = n_iter
        self.n_iter_per_test = n_iter_per_test
        self.tolerance = tolerance
        self.e_step_thresh = e_step_thresh

    def fit(self, X, y=None):
        self.fit_transform(X)
        return self

    def fit_transform(self, X, y=None):

        X = check_array(X, accept_sparse="csr")

        if not issparse(X):
            X = csr_matrix(X)

        U, V = plsa_fit(
            X,
            self.n_components,
            self.init,
            self.n_iter,
            self.n_iter_per_test,
            self.tolerance,
            self.e_step_thresh,
        )
        self.components_ = V
        self.embedding_ = U
        self.training_data_ = X

        return U

    def transform(self, X, y=None):
        X = check_array(X, accept_sparse="csr")

        if not issparse(X):
            X = coo_matrix(X)
        else:
            X = X.tocoo()

        n, m = X.shape

        result = plsa_refit(
            X.row,
            X.col,
            X.vals,
            n,
            m,
            self.components_,
            n_iter=50,
            n_iter_per_test=5,
            tolerance=0.001,
        )

        return result

    def coherence(self, topic_num=None, n_words=20):

        # Test for errors
        if not isinstance(topic_num, int) and topic_num is not None:
            raise ValueError("Topic number must be an integer or None.")

        if topic_num is None:
            return mean_coherence(self.components_, self.training_data_, n_words)
        elif topic_num >= 0 and topic_num < self.n_components:
            return coherence(self.components_, topic_num, self.training_data_, n_words)
        else:
            raise ValueError(
                "Topic number must be in range 0 to {}".format(self.n_components)
            )

    def log_lift(self, topic_num=None, n_words=20):

        # Test for errors
        if not isinstance(topic_num, int) and topic_num is not None:
            raise ValueError("Topic number must be an integer or None.")

        if topic_num is None:
            return mean_log_lift(self.components_, self.training_data_, n_words)
        elif topic_num >= 0 and topic_num < self.n_components:
            return log_lift(self.components_, topic_num, self.training_data_, n_words)
        else:
            raise ValueError(
                "Topic number must be in range 0 to {}".format(self.n_components)
            )