from functools import partial
from typing import Dict, Sequence, Tuple

import gym
import jax
import jax.numpy as jnp
import optax
from flax import struct
from flax.core import FrozenDict
from flax.training.train_state import TrainState

from supe.data.dataset import DatasetDict
from supe.networks import MLP, StateActionFeature
from supe.types import PRNGKey


class RND(struct.PyTreeNode):
    rng: PRNGKey
    net: TrainState
    init_net: TrainState
    frozen_net: TrainState
    coeff: float = struct.field(pytree_node=False)

    @classmethod
    def create(
        cls,
        seed: int,
        observation_space: gym.Space,
        action_space: gym.Space,
        lr: float = 3e-4,
        coeff: float = 1.0,
        hidden_dims: Sequence[int] = (256, 256),
        feature_dim: int = 256,
    ):

        observations = observation_space.sample()
        actions = action_space.sample()

        rng = jax.random.PRNGKey(seed)
        rng, key1, key2 = jax.random.split(rng, 3)

        net_cls = partial(MLP, hidden_dims=hidden_dims, activate_final=True)
        net_def = StateActionFeature(base_cls=net_cls, feature_dim=feature_dim)
        params = FrozenDict(net_def.init(key1, observations, actions)["params"])
        net = TrainState.create(
            apply_fn=net_def.apply,
            params=params,
            tx=optax.adam(learning_rate=lr),
        )
        frozen_params = FrozenDict(net_def.init(key2, observations, actions)["params"])
        frozen_net = TrainState.create(
            apply_fn=net_def.apply,
            params=frozen_params,
            tx=optax.adam(learning_rate=lr),
        )
        return cls(
            rng=rng,
            init_net=net,
            net=net,
            frozen_net=frozen_net,
            coeff=coeff,
        )

    @jax.jit
    def reset(self):
        return self.replace(net=self.init_net)

    @jax.jit
    def update(self, batch: DatasetDict) -> Tuple[struct.PyTreeNode, Dict[str, float]]:
        def loss_fn(params) -> Tuple[jnp.ndarray, Dict[str, float]]:
            feats = self.net.apply_fn(
                {"params": params}, batch["observations"], batch["actions"]
            )
            frozen_feats = self.frozen_net.apply_fn(
                {"params": self.frozen_net.params},
                batch["observations"],
                batch["actions"],
            )
            loss = ((feats - frozen_feats) ** 2.0).mean()
            return loss, {"rnd_loss": loss}

        grads, info = jax.grad(loss_fn, has_aux=True)(self.net.params)
        net = self.net.apply_gradients(grads=grads)

        return self.replace(net=net), info

    @partial(jax.jit, static_argnames=("stats",))
    def get_reward(self, observations, actions, stats=False):
        feats = self.net.apply_fn({"params": self.net.params}, observations, actions)
        frozen_feats = self.net.apply_fn(
            {"params": self.frozen_net.params}, observations, actions
        )
        reward = jnp.mean((feats - frozen_feats) ** 2.0, axis=-1) * self.coeff
        if stats:
            stats = {
                "mean": jnp.mean(reward),
                "std": jnp.std(reward),
                "max": jnp.max(reward),
                "min": jnp.min(reward),
            }
            return reward, stats
        return reward
