from __future__ import absolute_import, division, print_function

"""Implements blaze._elwise_eval function.

This function is meant to do OOC operations following a different
strategy than the canonical Blaze approach, and should be phased out as
soon as the canonical approach can do these sort of things efficiently.
"""

import sys, math
from dynd import nd, ndt
from .. import array, empty
import datashape

if sys.version_info >= (3, 0):
    xrange = range
    def dict_viewkeys(d):
        return d.keys()
else:
    def dict_viewkeys(d):
        return d.iterkeys()

min_numexpr_version = '2.2'  # the minimum version of Numexpr needed
numexpr_here = False
try:
    import numexpr
except ImportError:
    pass
else:
    if numexpr.__version__ >= min_numexpr_version:
        numexpr_here = True

if numexpr_here:
    import numexpr
    from numexpr.expressions import functions as numexpr_functions


class Defaults(object):
    """Class to taylor the setters and getters of default values."""

    def __init__(self):
        self.choices = {}

        # Choices setup
        self.choices['eval_vm'] = ("numexpr", "python")

    def check_choices(self, name, value):
        if value not in self.choices[name]:
            raiseValue, "value must be either 'numexpr' or 'python'"

    #
    # Properties start here...
    #

    @property
    def eval_vm(self):
        return self.__eval_vm

    @eval_vm.setter
    def eval_vm(self, value):
        self.check_choices('eval_vm', value)
        if value == "numexpr" and not numexpr_here:
            raise ValueError(
                   "cannot use `numexpr` virtual machine "
                   "(minimum required version is probably not installed)")
        self.__eval_vm = value


defaults = Defaults()


# Default values start here...

if numexpr_here:
    defaults.eval_vm = "numexpr"
else:
    defaults.eval_vm = "python"
"""
The virtual machine to be used in computations (via `eval`).  It can
be 'numexpr' or 'python'.  Default is 'numexpr', if installed.  If
not, then the default is 'python'.

"""

def _elwise_eval(expression, vm=None, user_dict={}, **kwargs):
    """
    eval(expression, vm=None, user_dict=None, **kwargs)

    Evaluate an `expression` and return the result.

    Parameters
    ----------
    expression : string
        A string forming an expression, like '2*a+3*b'. The values for 'a' and
        'b' are variable names to be taken from the calling function's frame.
        These variables may be scalars or Blaze arrays.
    vm : string
        The virtual machine to be used in computations.  It can be 'numexpr'
        or 'python'.  The default is to use 'numexpr' if it is installed.
    user_dict : dict
        An user-provided dictionary where the variables in expression
        can be found by name.
    kwargs : list of parameters or dictionary
        Any parameter supported by the blaze.array constructor.
        Useful for setting properties of the returned array object.

    Returns
    -------
    out : array object
        The outcome of the expression.  You can tailor the
        properties of this array by passing additional arguments
        supported by blaze.array constructor in `kwargs`.

    """

    if vm is None:
        vm = defaults.eval_vm
    if vm not in ("numexpr", "python"):
        raiseValue, "`vm` must be either 'numexpr' or 'python'"

    # Get variables and column names participating in expression
    depth = kwargs.pop('depth', 2)
    vars = _getvars(expression, user_dict, depth, vm=vm)

    # Gather info about sizes and lengths
    typesize, vlen = 0, 1
    for name in dict_viewkeys(vars):
        var = vars[name]
        # Scalars
        if not hasattr(var, "__len__"):
            continue
        if not hasattr(var, "dshape"):
            try:
                var = array(var)
            except:
                raise ValueError(
                    "sequence cannot be converted into a blaze array")
        # From now on, we only have Blaze arrays
        typesize += var.dshape.measure.itemsize
        # Check for length
        if vlen > 1 and vlen != len(var):
            raise ValueError("arrays must have the same length")
        vlen = len(var)

    if typesize == 0 or vlen == 0:
        # All scalars or zero-length objects
        if vm == "python":
            return eval(expression, vars)
        else:
            return numexpr.evaluate(expression, local_dict=vars)

    return _eval_blocks(expression, vars, vlen, typesize, vm, **kwargs)

def _getvars(expression, user_dict, depth, vm):
    """Get the variables in `expression`.

    `depth` specifies the depth of the frame in order to reach local
    or global variables.
    """

    cexpr = compile(expression, '<string>', 'eval')
    if vm == "python":
        exprvars = [ var for var in cexpr.co_names
                     if var not in ['None', 'False', 'True'] ]
    else:
        # Check that var is not a numexpr function here.  This is useful for
        # detecting unbound variables in expressions.  This is not necessary
        # for the 'python' engine.
        exprvars = [ var for var in cexpr.co_names
                     if var not in ['None', 'False', 'True']
                     and var not in numexpr_functions ]

    # Get the local and global variable mappings of the user frame
    user_locals, user_globals = {}, {}
    user_frame = sys._getframe(depth)
    user_locals = user_frame.f_locals
    user_globals = user_frame.f_globals

    # Look for the required variables
    reqvars = {}
    for var in exprvars:
        # Get the value
        if var in user_dict:
            val = user_dict[var]
        elif var in user_locals:
            val = user_locals[var]
        elif var in user_globals:
            val = user_globals[var]
        else:
            if vm == "numexpr":
                raise NameError("variable name ``%s`` not found" % var)
            val = None
        # Check the value
        if (vm == "numexpr" and
            hasattr(val, 'dshape') and
            val.dshape.measure.name == 'uint64'):
            raise NotImplementedError(
                "variable ``%s`` refers to "
                "a 64-bit unsigned integer object, that is "
                "not yet supported in numexpr expressions; "
                "rather, use the 'python' vm." % var )
        if val is not None:
            reqvars[var] = val
    return reqvars

def _eval_blocks(expression, vars, vlen, typesize, vm, **kwargs):
    """Perform the evaluation in blocks."""

    # Compute the optimal block size (in elements)
    # The next is based on experiments, but YMMV
    if vm == "numexpr":
        # If numexpr, make sure that operands fit in L3 chache
        bsize = 2**20  # 1 MB is common for L3
    else:
        # If python, make sure that operands fit in L2 chache
        bsize = 2**17  # 256 KB is common for L2
    bsize //= typesize
    # Evaluation seems more efficient if block size is a power of 2
    bsize = 2 ** (int(math.log(bsize, 2)))
    if vlen < 100*1000:
        bsize //= 8
    elif vlen < 1000*1000:
        bsize //= 4
    elif vlen < 10*1000*1000:
        bsize //= 2
    # Protection against too large atomsizes
    if bsize == 0:
        bsize = 1

    vars_ = {}
    # Get temporaries for vars
    maxndims = 0
    for name in dict_viewkeys(vars):
        var = vars[name]
        if hasattr(var, "__len__"):
            if not hasattr(var, "dshape"):
                # Convert sequences into regular Blaze arrays
                var = array(var)
            ndims = len(var.dshape.shape)
            if ndims > maxndims:
                maxndims = ndims
            if len(var) > bsize:
                vars_[name] = nd.empty(bsize, var.dshape.measure.name)

    for i in xrange(0, vlen, bsize):
        # Correction for the block size
        if i+bsize > vlen:
            bsize = vlen - i
        # Get buffers for vars
        for name in dict_viewkeys(vars):
            var = vars[name]
            if hasattr(var, "__len__") and len(var) > bsize:
                vars_[name] = var[i:i+bsize]
            else:
                if hasattr(var, "__getitem__"):
                    vars_[name] = var[:]
                else:
                    vars_[name] = var

        # Perform the evaluation for this block
        if vm == "python":
            res_block = eval(expression, vars_)
        else:
            res_block = numexpr.evaluate(expression, local_dict=vars_)

        if i == 0:
            # Detection of reduction operations
            scalar = False
            dim_reduction = False
            if len(res_block.shape) == 0:
                scalar = True
                result = res_block
                continue
            elif len(res_block.shape) < maxndims:
                dim_reduction = True
                result = res_block
                continue
            out_shape = list(res_block.shape)
            out_shape[0] = vlen
            dshape = datashape.from_numpy(out_shape, res_block.dtype)
            result = empty(dshape, **kwargs)
            result[:bsize] = res_block
        else:
            if scalar or dim_reduction:
                result += res_block
            result[i:i+bsize] = res_block

    if scalar:
        return result[()]
    return result
