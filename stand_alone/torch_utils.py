# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch.nn
import torch.optim
from tensordict.nn.distributions import NormalParamExtractor
from tensordict.nn import TensorDictModule
from torchrl.data import CompositeSpec
from torchrl.data.tensor_specs import DiscreteBox
from torchrl.envs import (
    CatFrames,
    DoubleToFloat,
    EndOfLifeTransform,
    EnvCreator,
    ExplorationType,
    GrayScale,
    GymEnv,
    NoopResetEnv,
    ParallelEnv,
    Resize,
    RewardSum,
    SignTransform,
    StepCounter,
    ToTensorImage,
    TransformedEnv,
    VecNorm,
)
from torchrl.modules import (
    ActorValueOperator,
    ConvNet,
    MLP,
    OneHotCategorical,
    ProbabilisticActor,
    TanhNormal,
    ValueOperator,
)

from torchrl.envs import BraxWrapper
import brax.envs as brax_envs
from custom_torchrl_env import RodentRunEnv

from Rodent_Env_Brax import Rodent
# ====================================================================
# Environment utils
# --------------------------------------------------------------------


# def make_env(env_name="rodent", frame_skip=4, is_test=False):
#     brax_envs.register_environment(env_name, Rodent)
#     env = BraxWrapper(brax_envs.get_environment(env_name), 
#                       iterations=6,
#                       ls_iterations=3)
#     env.set_seed(0)
#     env = TransformedEnv(env)
#     return env

def make_env(env_name, device="cpu"):
     # env_name not in use now, can be use later

    env = RodentRunEnv(device=device)
    env._set_seed(0)
    env = TransformedEnv(env)
    env.append_transform(RewardSum())
    env.append_transform(StepCounter())

    return env


# def make_parallel_env(env_name, num_envs, device, is_test=False):
#     env = ParallelEnv(
#         num_envs,
#         EnvCreator(lambda: make_env(env_name)),
#         serial_for_single=True,
#         device=device,
#     )
#     env = TransformedEnv(env)
#     env.append_transform(VecNorm(in_keys=["observation"]))
#     env.append_transform(RewardSum())
#     return env


# ====================================================================
# Model utils
# --------------------------------------------------------------------


def make_ppo_modules_pixels(proof_environment):

    # Define input shape
    input_shape = proof_environment.observation_spec["observation"].shape

    # Define distribution class and kwargs
    if isinstance(proof_environment.action_spec.space, DiscreteBox):
        num_outputs = proof_environment.action_spec.space.n
        distribution_class = OneHotCategorical
        distribution_kwargs = {}
    else:  # is ContinuousBox
        num_outputs = 2 * proof_environment.action_spec.shape[-1] # emil's env implementation require this, ask around!
        distribution_class = TanhNormal
        distribution_kwargs = {
            "min": proof_environment.action_spec.space.low,
            "max": proof_environment.action_spec.space.high,
        }

    # Define input keys
    in_keys = ["observation"]

    # # Define a shared Module and TensorDictModule (CNN + MLP)
    # common_cnn = ConvNet(
    #     activation_class=torch.nn.ReLU,
    #     num_cells=[32, 64, 64],
    #     kernel_sizes=[8, 4, 3],
    #     strides=[4, 2, 1],
    # )
    
    # common_cnn_output = common_cnn(torch.ones(input_shape))
    
    common_mlp = MLP(
        in_features=input_shape[-1], #common_cnn_output.shape[-1],
        activation_class=torch.nn.ReLU,
        activate_last_layer=True,
        out_features=512,
        num_cells=[],
    )
    
    common_mlp_output = common_mlp(torch.ones(input_shape))#(common_cnn_output)

    # Define shared net as TensorDictModule
    common_module = TensorDictModule(
        module=torch.nn.Sequential(#common_cnn,
                                   common_mlp,
                                   ),
        in_keys=in_keys,
        out_keys=["common_features"],
    )

    # Define on head for the policy
    policy_mlp = MLP(
        in_features=common_mlp_output.shape[-1],
        out_features=num_outputs,
        activation_class=torch.nn.ReLU,
        num_cells=[],
    )

    policy_net = torch.nn.Sequential(policy_mlp,
                                     NormalParamExtractor(),)
    
    policy_module = TensorDictModule(
        module=policy_net,
        in_keys=["common_features"],
        out_keys=["loc", "scale"],
    )

    # Add probabilistic sampling of the actions
    policy_module = ProbabilisticActor(
        policy_module,
        in_keys=["loc", "scale"],
        spec=CompositeSpec(action=proof_environment.action_spec),
        distribution_class=distribution_class,
        distribution_kwargs=distribution_kwargs,
        return_log_prob=True,
        default_interaction_type=ExplorationType.RANDOM,
    )

    # Define another head for the value
    value_net = MLP(
        activation_class=torch.nn.ReLU,
        in_features=common_mlp_output.shape[-1],
        out_features=1,
        num_cells=[],
    )
    value_module = ValueOperator(
        value_net,
        in_keys=["common_features"],
    )

    return common_module, policy_module, value_module


def make_ppo_models(env_name):

    proof_environment = make_env(env_name,device="cpu")
    common_module, policy_module, value_module = make_ppo_modules_pixels(
        proof_environment
    )

    # Wrap modules in a single ActorCritic operator
    actor_critic = ActorValueOperator(
        common_operator=common_module,
        policy_operator=policy_module,
        value_operator=value_module,
    )

    with torch.no_grad():
        td = proof_environment.rollout(max_steps=100, break_when_any_done=False)
        td = actor_critic(td)
        del td

    actor = actor_critic.get_policy_operator()
    critic = actor_critic.get_value_operator()

    del proof_environment

    return actor, critic


# ====================================================================
# Evaluation utils
# --------------------------------------------------------------------


def eval_model(actor, test_env, num_episodes=3):
    test_rewards = []
    for _ in range(num_episodes):
        td_test = test_env.rollout(
            policy=actor,
            auto_reset=True,
            auto_cast_to_device=True,
            break_when_any_done=True,
            max_steps=10_000_000,
        )
        reward = td_test["next", "episode_reward"][td_test["next", "done"]]
        test_rewards.append(reward.cpu())
    del td_test
    return torch.cat(test_rewards, 0).mean()