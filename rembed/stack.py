"""Theano-based stack implementations."""


import numpy as np
import theano
from theano import tensor as T

from rembed import util


def update_hard_stack(stack_t, stack_cur_t, stack_pushed, stack_merged,
                      push_value, merge_value, mask):
    """Compute the new value of the given hard stack.

    This performs stack pushes and pops in parallel, and somewhat wastefully.
    It accepts a precomputed merge result (in `merge_value`) and a precomputed
    push value `push_value` for all examples, and switches between the two
    outcomes based on the per-example value of `mask`.

    Args:
        stack_t: Current stack value
        stack_pushed: Helper stack structure, of same size as `stack_t`
        stack_merged: Helper stack structure, of same size as `stack_t`
        push_value: Batch of values to be pushed
        merge_value: Batch of merge results
        mask: Batch of booleans: 1 if merge, 0 if push
    """

    batch_idxs = T.arange(stack_t.shape[0])

    # TODO(jgauthier): how to tell set_subtensor to write updated value into
    # existing helper memory stack_merged, stack_pushed? Something like numpy's
    # `out` kwarg.

    # Build two copies of the stack batch: one where every stack has received
    # a push op, and one where every stack has received a merge op.
    #
    # Copy 1: Push.
    stack_pushed = T.set_subtensor(stack_t[batch_idxs, stack_cur_t], push_value)

    # Copy 2: Merge.
    stack_merged = T.set_subtensor(stack_t[batch_idxs, stack_cur_t - 2], merge_value)
    stack_merged = T.set_subtensor(stack_merged[batch_idxs, stack_cur_t - 1], 0.0)

    # Make sure mask broadcasts over all dimensions after the first.
    mask = mask.dimshuffle(0, "x", "x")
    mask = T.cast(mask, dtype=theano.config.floatX)
    stack_next = mask * stack_merged + (1. - mask) * stack_pushed

    return stack_next


class HardStack(object):

    """
    Model 0/1/2 hard stack implementation.

    This model scans a sequence using a hard stack. It optionally predicts
    stack operations using an MLP, and can receive supervision on these
    predictions from some external parser which acts as the "ground truth"
    parser.

    Model 0: predict_network=None, use_predictions=False
    Model 1: predict_network=something, use_predictions=False
    Model 2: predict_network=something, use_predictions=True
    """

    def __init__(self, embedding_dim, vocab_size, seq_length, compose_network,
                 embedding_projection_network, apply_dropout, vs, predict_network=None,
                 use_predictions=False, X=None, transitions=None, initial_embeddings=None,
                 embedding_dropout_keep_rate=1.0):
        """
        Construct a HardStack.

        Args:
            embedding_dim: Dimensionality of token embeddings and stack values
            vocab_size: Number of unique tokens in vocabulary
            seq_length: Maximum sequence length which will be processed by this
              stack
            compose_network: Blocks-like function which accepts arguments
              `inp, inp_dim, outp_dim, vs, name` (see e.g. `util.Linear`).
              Given a Theano batch `inp` of dimension `batch_size * inp_dim`,
              returns a transformed Theano batch of dimension
              `batch_size * outp_dim`.
            embedding_projection_network: Same form as `compose_network`.
            apply_dropout: A Theano scalar indicating whether to apply dropout (1.0)
              or eval-mode rescaling (0.0).
            vs: VariableStore instance for parameter storage
            predict_network: Blocks-like function which maps values
              `3 * embedding_dim` to `action_dim`
            use_predictions: If `True`, use the predictions from the model
              (rather than the ground-truth `transitions`) to perform stack
              operations
            X: Theano batch describing input matrix, or `None` (in which case
              this instance will make its own batch variable).
            transitions: Theano batch describing transition matrix, or `None`
              (in which case this instance will make its own batch variable).
            embedding_dropout_keep_rate: The keep rate for dropout on projected
              embeddings.
        """

        self.embedding_dim = embedding_dim
        self.vocab_size = vocab_size
        self.seq_length = seq_length

        self._compose_network = compose_network
        self._embedding_projection_network = embedding_projection_network
        self._predict_network = predict_network
        self.use_predictions = use_predictions

        self._vs = vs

        self.initial_embeddings = initial_embeddings

        self.apply_dropout = apply_dropout
        self.embedding_dropout_keep_rate = embedding_dropout_keep_rate

        self.X = X
        self.transitions = transitions

        self._make_params()
        self._make_inputs()
        self._make_scan()

        self.scan_fn = theano.function([self.X, self.transitions, self.apply_dropout],
                                       self.final_stack)

    def _make_params(self):
        # Per-token embeddings.
        if self.initial_embeddings is not None:
            def EmbeddingInitializer(shape):
                return self.initial_embeddings
            self.embeddings = self._vs.add_param(
                "embeddings", (self.vocab_size, self.embedding_dim), initializer=EmbeddingInitializer)
        else:
            self.embeddings = self._vs.add_param(
                "embeddings", (self.vocab_size, self.embedding_dim))

    def _make_inputs(self):
        self.X = self.X or T.imatrix("X")
        self.transitions = self.transitions or T.imatrix("transitions")

    def _make_scan(self):
        """Build the sequential composition / scan graph."""

        batch_size, max_stack_size = self.X.shape

        # Stack batch is a 3D tensor.
        stack_shape = (batch_size, max_stack_size, self.embedding_dim)
        stack_init = T.zeros(stack_shape)

        # Allocate two helper stack copies (passed as non_seqs into scan).
        stack_pushed = T.zeros(stack_shape)
        stack_merged = T.zeros(stack_shape)

        # Maintain a stack cursor for each example. The stack cursor points to
        # the next push location on the stack (i.e., the first empty element at
        # the top of the stack.)
        stack_cur_init = T.zeros((batch_size,), dtype="int")

        # Look up all of the embeddings that will be used.
        raw_embeddings = self.embeddings[self.X]  # batch_size * seq_length * emb_dim

        # Allocate a "buffer" stack initialized with projected embeddings,
        # and maintain a cursor in this buffer.
        buffer_t = self._embedding_projection_network(
            raw_embeddings, self.embedding_dim, self.embedding_dim, self._vs, name="project")
        buffer_t = util.Dropout(buffer_t, self.embedding_dropout_keep_rate, self.apply_dropout)

        buffer_cur_init = T.zeros((batch_size,), dtype="int")

        # TODO(jgauthier): Implement linear memory (was in previous HardStack;
        # dropped it during a refactor)

        def step(transitions_t, stack_t, stack_cur_t, buffer_cur_t, stack_pushed,
                 stack_merged, buffer):
            # Extract top buffer values.
            buffer_top_t = buffer_t[T.arange(batch_size), buffer_cur_t]

            if self._predict_network is not None:
                # We are predicting our own stack operations.
                predict_inp = T.concatenate(
                    [stack_t[:, 0], stack_t[:, 1], buffer_top_t], axis=1)
                actions_t = self._predict_network(
                    predict_inp, self.embedding_dim * 3, 2, self._vs,
                    name="predict_actions")

            if self.use_predictions:
                # Use predicted actions to build a mask.
                mask = actions_t.argmax(axis=1)
            else:
                # Use transitions provided from external parser.
                mask = transitions_t

            # Now update the stack: first precompute merge results.
            stack_idxs = T.repeat(stack_cur_t[:, np.newaxis], 2, axis=1) + [-1, -2]
            stack_idxs = stack_idxs.flatten()
            batch_idxs = T.repeat(T.arange(batch_size), 2)
            merge_items = stack_t[batch_idxs, stack_idxs]
            merge_items = merge_items.reshape((-1, self.embedding_dim * 2))
            merge_value = self._compose_network(
                merge_items, self.embedding_dim * 2, self.embedding_dim,
                self._vs, name="compose")

            # Compute new stack value.
            stack_next = update_hard_stack(
                stack_t, stack_cur_t, stack_pushed, stack_merged, buffer_top_t,
                merge_value, mask)

            # Move stack cursor. (Shift -1 after merging (mask = 1); shift +1
            # after pushing (mask = 0).)
            stack_cur_next = stack_cur_t + mask * -2 + 1
            stack_cur_next = T.maximum(0, stack_cur_next)

            # Move buffer cursor as necessary. Since mask == 1 when merge, we
            # should increment each buffer cursor by 1 - mask
            buffer_cur_next = buffer_cur_t + (1 - mask)

            if self._predict_network is not None:
                return stack_next, actions_t, stack_cur_next, buffer_cur_next
            else:
                return stack_next, stack_cur_next, buffer_cur_next

        # Dimshuffle inputs to seq_len * batch_size for scanning
        transitions = self.transitions.dimshuffle(1, 0)

        # If we have a prediction network, we need an extra outputs_info
        # element (the `None`) to carry along prediction values
        if self._predict_network is not None:
            outputs_info = [stack_init, None, stack_cur_init, buffer_cur_init]
        else:
            outputs_info = [stack_init, stack_cur_init, buffer_cur_init]

        scan_ret = theano.scan(
            step, transitions,
            non_sequences=[stack_pushed, stack_merged, buffer_t],
            outputs_info=outputs_info)[0]

        self.final_stack = scan_ret[0][-1]

        self.transitions_pred = None
        if self._predict_network is not None:
            self.transitions_pred = scan_ret[1].dimshuffle(1, 0, 2)


class Model0(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = None
        kwargs["use_predictions"] = False
        super(Model0, self).__init__(*args, **kwargs)


class Model1(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = kwargs.get("predict_network", util.Linear)
        kwargs["use_predictions"] = False
        super(Model1, self).__init__(*args, **kwargs)


class Model2(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = kwargs.get("predict_network", util.Linear)
        kwargs["use_predictions"] = True
        super(Model2, self).__init__(*args, **kwargs)
