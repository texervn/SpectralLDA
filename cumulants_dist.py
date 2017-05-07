''' Cumulants computations in distributed mode

'''
from itertools import islice
import numpy as np
from cumulants import (equal_partitions, contrib_m1, contrib_prod_e2_x,
                       contrib_whiten_e3, contrib_whiten_e2m1)
import mxnet as mx
from partitioned_data import pload

KVSTORE = mx.kvstore.create('dist')
KEY_M1 = 100
KEY_PROD_M2X = 110
KEY_WHITENED_E3 = 120
KEY_WHITENED_E2M1 = 130


def nth(iterable, n, default=None):
    "Returns the nth item or a default value"
    # pylint: disable=invalid-name
    return next(islice(iterable, n, None), default)

def moment1_dist(docs, n_partitions=1):
    ''' Compute M1 in distributed mode

    Parameters
    -----------
    docs : str
        Path for the entire collection of word count vectors.
    n_partitions : int
        Number of partitions over the document collection, >= 1.

    Returns
    ----------
    out : length-vocab_size array
        M1 of the entire document collection
    '''
    n_docs, vocab_size = docs.shape
    assert n_docs >= 1 and vocab_size >= 1
    assert n_partitions >= 1 and n_partitions <= n_docs

    kvstore = mx.kvstore.create('local')
    m1_mx = mx.nd.zeros((vocab_size,))
    kvstore.init(KEY_M1, m1_mx)

    start, end = nth(equal_partitions(n_docs, n_partitions), kvstore.rank)
    curr_partition = pload(docs, start, end)
    contrib = contrib_m1(curr_partition, n_docs)
    kvstore.push(KEY_M1, mx.nd.array(contrib))

    kvstore.pull(KEY_M1, out=m1_mx)
    kvstore.close()
    return m1_mx.asnumpy()

def prod_m2_x_dist(docs, test_x, alpha0, docs_m1=None, n_partitions=1):
    ''' Compute the product of M2 by test matrix X in distributed mode

    Parameters
    -----------
    docs : str
        Path for the entire collection of word count vectors.
    test_x : vocab_size-by-k array
        Test matrix where k is the number of factors.
    alpha0 : float
        Sum of the Dirichlet prior parameter.
    docs_m1: length-vocab_size array, optional
        M1 of the entire collection of word count vectors.
    n_partitions : int, optional
        Number of partitions, 1 by default.

    Returns
    -----------
    out : vocab_size-by-k array
        Product of M2 by X.
    '''
    def adjust(prod_e2x, docs_m1, test_x, alpha0):
        ''' Adjust for the final result '''
        adj = alpha0 / (alpha0 + 1) * np.outer(docs_m1, docs_m1.dot(test_x))
        return prod_e2x - adj

    n_docs, vocab_size = docs.shape
    _vocab_size, num_factors = test_x.shape
    assert n_docs >= 1 and vocab_size >= 1
    assert n_partitions >= 1 and n_partitions <= n_docs
    assert vocab_size == _vocab_size and num_factors >= 1
    if docs_m1 is not None:
        assert docs_m1.ndim == 1 and vocab_size == len(docs_m1)
    assert alpha0 > 0

    # Compute M1 if not provided
    if docs_m1 is None:
        docs_m1 = moment1_dist(docs, n_partitions)

    # Init KVStore
    kvstore = mx.kvstore.create('local')
    prod_e2x_mx = mx.nd.zeros((vocab_size, num_factors))
    kvstore.init(KEY_PROD_M2X, prod_e2x_mx)

    # Push current contribution to product of E2 and X
    start, end = nth(equal_partitions(n_docs, n_partitions), kvstore.rank)
    curr_partition = pload(docs, start, end)
    contrib = contrib_prod_e2_x(curr_partition, test_x, n_docs)
    kvstore.push(KEY_PROD_M2X, mx.nd.array(contrib))

    # Reduce and pull the product of E2 and X
    kvstore.pull(KEY_PROD_M2X, out=prod_e2x_mx)
    kvstore.close()

    return adjust(prod_e2x_mx.asnumpy(), docs_m1, test_x, alpha0)

def whiten_m3_dist(docs, whn, alpha0, docs_m1=None, n_partitions=1):
    ''' Whiten M3 in distributed mode

    Parameters
    -----------
    docs : str
        Path for the entire collection of word count vectors.
    whn : vocab_size-by-k array
        Whitening matrix.
    alpha0 : float
        Sum of Dirichlet prior parameter.
    docs_m1 : length-vocab_size array, optional
        M1 of the entire collection of word count vectors.
    n_partitions : int, optional
        Number of partitions, 1 by default.

    Returns
    ----------
    out : k-by-(k ** 2) array
        Whitened M3, unfolded version.
    '''
    def adjust(whitened_e3, whitened_e2m1, docs_m1, whn, alpha0):
        ''' Adjust for the final result '''
        _, num_factors = whn.shape
        # length-k
        whitened_m1 = docs_m1.dot(whn)
        whitened_m1_3 = (np.einsum('i,j,k->ijk', whitened_m1, whitened_m1,
                                   whitened_m1).reshape((num_factors, -1)))

        coeff1 = alpha0 / (alpha0 + 2)
        coeff2 = 2 * alpha0 ** 2 / (alpha0 + 1) / (alpha0 + 2)
        return (whitened_e3 - coeff1 * whitened_e2m1
                + coeff2 * whitened_m1_3)

    n_docs, vocab_size = docs.shape
    _vocab_size, num_factors = whn.shape
    assert n_docs >= 1 and vocab_size >= 1
    assert n_partitions <= n_docs
    assert vocab_size == _vocab_size and num_factors >= 1
    if docs_m1 is not None:
        assert docs_m1.ndim == 1 and vocab_size == len(docs_m1)
    assert alpha0 > 0

    # Compute M1 if not provided
    if docs_m1 is None:
        docs_m1 = moment1_dist(docs, n_partitions)

    # Init KVStore
    kvstore = mx.kvstore.create('local')
    whitened_e3_mx = mx.nd.zeros((num_factors, num_factors ** 2))
    whitened_e2m1_mx = mx.nd.zeros((num_factors, num_factors ** 2))
    kvstore.init(KEY_WHITENED_E3, whitened_e3_mx)
    kvstore.init(KEY_WHITENED_E2M1, whitened_e2m1_mx)

    # Push current contribution to product of E2 and X
    start, end = nth(equal_partitions(n_docs, n_partitions), kvstore.rank)
    curr_partition = pload(docs, start, end)
    contrib_e3 = contrib_whiten_e3(curr_partition, whn, n_docs)
    contrib_e2m1 = contrib_whiten_e2m1(curr_partition, docs_m1,
                                       whn, n_docs)
    kvstore.push(KEY_WHITENED_E3, mx.nd.array(contrib_e3))
    kvstore.push(KEY_WHITENED_E2M1, mx.nd.array(contrib_e2m1))

    # Reduce and pull the product of E2 and X
    kvstore.pull(KEY_WHITENED_E3, out=whitened_e3_mx)
    kvstore.pull(KEY_WHITENED_E2M1, out=whitened_e2m1_mx)
    kvstore.close()

    return adjust(whitened_e3_mx.asnumpy(), whitened_e2m1_mx.asnumpy(),
                  docs_m1, whn, alpha0)