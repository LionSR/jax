# Copyright 2023 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module for Pallas:TPU-specific JAX primitives and functions."""
from __future__ import annotations

import contextlib
import dataclasses
from typing import Any, Callable

import jax
from jax._src import api_util
from jax._src import core as jax_core
from jax._src import effects
from jax._src import linear_util as lu
from jax._src import tree_util
from jax._src import util
from jax._src.interpreters import mlir
from jax._src.interpreters import partial_eval as pe
from jax._src.pallas import indexing
from jax._src.pallas.mosaic import core as tpu_core
import jax.numpy as jnp

map, unsafe_map = util.safe_map, map
zip, unsafe_zip = util.safe_zip, zip

repeat_p = jax_core.Primitive('repeat')

def repeat(x, repeats, axis):
  return repeat_p.bind(x, repeats=repeats, axis=axis)

@repeat_p.def_abstract_eval
def _repeat_abstract_eval(x, *, repeats, axis):
  shape = list(x.shape)
  shape[axis] *= repeats
  return jax_core.ShapedArray(shape, x.dtype)


def _repeat_lowering_rule(ctx: mlir.LoweringRuleContext, x, *, repeats, axis):
  def _repeat(x):
    return jnp.repeat(x, repeats, axis)
  return mlir.lower_fun(_repeat, multiple_results=False)(ctx, x)
mlir.register_lowering(repeat_p, _repeat_lowering_rule)

trace_start_p = jax_core.Primitive('trace_start')
trace_start_p.multiple_results = True


@trace_start_p.def_abstract_eval
def _trace_start_abstract_eval(*, message: str, level: int):
  del message, level
  return []


trace_stop_p = jax_core.Primitive('trace_stop')
trace_stop_p.multiple_results = True


@trace_stop_p.def_abstract_eval
def _trace_stop_abstract_eval():
  return []


@contextlib.contextmanager
def trace(message: str, level: int = 10):
  trace_start_p.bind(message=message, level=level)
  yield
  trace_stop_p.bind()


run_scoped_p = jax_core.Primitive('run_scoped')
run_scoped_p.multiple_results = True


def run_scoped(f: Callable[..., None], *types, **kw_types) -> None:
  flat_types, in_tree = tree_util.tree_flatten((types, kw_types))
  flat_fun, _ = api_util.flatten_fun(lu.wrap_init(f), in_tree)
  avals = map(lambda t: t.get_aval(), flat_types)
  jaxpr, _, consts = pe.trace_to_jaxpr_dynamic(flat_fun, avals)
  run_scoped_p.bind(*consts, jaxpr=jaxpr)


@run_scoped_p.def_effectful_abstract_eval
def _run_scoped_abstract_eval(*args, jaxpr):
  # jaxpr will have effects for its inputs (Refs that are allocated) and for
  # constvars (closed over Refs). The effects for the allocated Refs are local
  # to the jaxpr and shouldn't propagate out.
  nonlocal_effects = {
      eff for eff in jaxpr.effects
      if not (
          isinstance(eff, effects.JaxprInputEffect)
          and eff.input_index >= len(jaxpr.constvars)
      )
  }
  return [], nonlocal_effects


semaphore_signal_p = jax_core.Primitive('semaphore_signal')
semaphore_signal_p.multiple_results = True

def semaphore_signal(sem, inc: int | jax.Array = 1,
                     *, device_id: int | jax.Array | None = None):
  inc = jnp.asarray(inc, dtype=jnp.int32)
  args = [sem, inc]
  has_device_id = device_id is not None
  if has_device_id:
    args = [*args, device_id]
  semaphore_signal_p.bind(*args, has_device_id=has_device_id)

@semaphore_signal_p.def_abstract_eval
def _semaphore_signal_abstract_eval(sem_aval: tpu_core.AbstractSemaphore, value,
                                    *args, has_device_id: bool):
  if not isinstance(sem_aval, tpu_core.AbstractSemaphore):
    raise ValueError(f"Cannot signal on a non-semaphore value: {sem_aval}")
  if sem_aval.sem_type is not tpu_core.SemaphoreType.REGULAR:
    raise ValueError("Must signal a REGULAR semaphore.")
  if value.dtype != jnp.dtype("int32"):
    raise ValueError("Must signal an int32 value.")
  if has_device_id:
    (device_id,) = args
    if device_id.dtype != jnp.dtype("int32"):
      raise ValueError("`device_id` must be an int32 value.")
  return []

semaphore_wait_p = jax_core.Primitive('semaphore_wait')
semaphore_wait_p.multiple_results = True

def semaphore_wait(sem, dec: int | jax.Array = 1):
  dec = jnp.asarray(dec, dtype=jnp.int32)
  semaphore_wait_p.bind(sem, dec)

@semaphore_wait_p.def_abstract_eval
def _semaphore_wait_abstract_eval(sem_aval: tpu_core.AbstractSemaphore, value):
  if not isinstance(sem_aval, tpu_core.AbstractSemaphore):
    raise ValueError(f"Cannot wait on a non-semaphore value: {sem_aval}")
  if sem_aval.sem_type is not tpu_core.SemaphoreType.REGULAR:
    raise ValueError("Must wait a REGULAR semaphore.")
  if value.dtype != jnp.dtype("int32"):
    raise ValueError("Must signal an int32 value.")
  return []


@dataclasses.dataclass
class DMAFuture:
  flat_args: Any
  tree: Any

  def wait(self):
    dma_wait_p.bind(*self.flat_args, tree=self.tree)

dma_start_p = jax_core.Primitive('dma_start')
dma_start_p.multiple_results = True

@dma_start_p.def_abstract_eval
def _dma_start_abstract_eval(*args, tree):
  del args, tree
  return []

def dma_start(src_ref, src_indices, dst_ref, dst_indices, sem) -> DMAFuture:
  src_indexer = indexing.NDIndexer.from_indices_shape(src_indices,
                                                      src_ref.shape)
  dst_indexer = indexing.NDIndexer.from_indices_shape(dst_indices,
                                                      dst_ref.shape)
  args = (src_ref, src_indexer, dst_ref, dst_indexer, sem, None, None)
  flat_args, tree = tree_util.tree_flatten(args)
  dma_start_p.bind(*flat_args, tree=tree)
  wait_args, tree = tree_util.tree_flatten((sem, dst_ref, dst_indexer))
  return DMAFuture(wait_args, tree)


def remote_dma_start(src_ref, src_indices, dst_ref, dst_indices, src_sem,
                     dst_sem, device_id) -> tuple[DMAFuture, DMAFuture]:
  src_indexer = indexing.NDIndexer.from_indices_shape(src_indices,
                                                      src_ref.shape)
  dst_indexer = indexing.NDIndexer.from_indices_shape(dst_indices,
                                                      dst_ref.shape)
  args = (src_ref, src_indexer, dst_ref, dst_indexer, dst_sem, src_sem,
          device_id)
  flat_args, tree = tree_util.tree_flatten(args)
  dma_start_p.bind(*flat_args, tree=tree)
  recv_wait_args = (dst_sem, dst_ref, dst_indexer)
  recv_args, recv_tree = tree_util.tree_flatten(recv_wait_args)
  send_wait_args = (src_sem, src_ref, src_indexer)
  send_args, send_tree = tree_util.tree_flatten(send_wait_args)
  return DMAFuture(send_args, send_tree), DMAFuture(recv_args, recv_tree)


dma_wait_p = jax_core.Primitive('dma_wait')
dma_wait_p.multiple_results = True

@dma_wait_p.def_abstract_eval
def _dma_wait_abstract_eval(*args, tree):
  del args, tree
  return []

device_id_p = jax_core.Primitive('device_id')

@device_id_p.def_abstract_eval
def _device_id_abstract_eval():
  return jax_core.ShapedArray((), jnp.dtype("int32"))

device_id = device_id_p.bind
