# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch.nn
import torch.optim

from tensordict.nn import AddStateIndependentNormalScale, TensorDictModule
from torchrl.data import CompositeSpec
from torchrl.envs import (
    ClipTransform,
    DoubleToFloat,
    ExplorationType,
    RewardSum,
    StepCounter,
    TransformedEnv,
    VecNorm,
)
from custom_torchrl_env import RodentRunEnv
from torchrl.modules import (
    MLP,
    ProbabilisticActor,
    TanhNormal,
    ValueOperator,
    ConvNet)

import mujoco
import moviepy.editor
import numpy as np

# ====================================================================
# Environment utils
# --------------------------------------------------------------------


def make_env(batch_size, worker_threads, device="cpu"):
    env = RodentRunEnv(batch_size=batch_size, 
                       worker_thread_count=worker_threads,
                       device=device)
    env = TransformedEnv(env)
    #env.append_transform(VecNorm(in_keys=["observation"], decay=0.99999, eps=1e-2))
    env.append_transform(RewardSum())
    env.append_transform(StepCounter())
    return env


# ====================================================================
# Model utils
# --------------------------------------------------------------------


def make_ppo_models_state(proof_environment):
    '''No common module needed, network uses seperat pathway anyways'''

    # Define input shape
    input_shape = proof_environment.observation_spec["observation"].shape

    # Define policy output distribution class
    num_outputs = proof_environment.action_spec.shape[-1]
    distribution_class = TanhNormal
    distribution_kwargs = {
        "min": proof_environment.action_spec.space.low,
        "max": proof_environment.action_spec.space.high,
        "tanh_loc": False,
    }

     # # Define a shared Module and TensorDictModule (CNN + MLP)
    # common_cnn = ConvNet(
    #     activation_class=torch.nn.ReLU,
    #     num_cells=[32, 64, 64],
    #     kernel_sizes=[8, 4, 3],
    #     strides=[4, 2, 1],
    # )
    
    # common_cnn_output = common_cnn(torch.ones(input_shape))

    # Define policy architecture first, not passing in
    policy_mlp = MLP(
        in_features=input_shape[-1],
        activation_class=torch.nn.Tanh,
        out_features=num_outputs,  # predict only loc
        num_cells=[64, 64],
    )

    # Initialize policy weights
    for layer in policy_mlp.modules():
        if isinstance(layer, torch.nn.Linear):
            torch.nn.init.orthogonal_(layer.weight, 1.0)
            layer.bias.data.zero_()

    # Add state-independent normal scale, cover TanhNormal function
    policy_mlp = torch.nn.Sequential(
        policy_mlp,
        AddStateIndependentNormalScale(
            proof_environment.action_spec.shape[-1], scale_lb=1e-8
        ),
    )

    # Add probabilistic sampling of the actions & actually pass in now
    policy_module = ProbabilisticActor(
        TensorDictModule(
            module=policy_mlp,
            in_keys=["observation"],
            out_keys=["loc", "scale"],
        ),
        in_keys=["loc", "scale"],
        spec=CompositeSpec(action=proof_environment.action_spec),
        distribution_class=distribution_class,
        distribution_kwargs=distribution_kwargs,
        return_log_prob=True,
        default_interaction_type=ExplorationType.RANDOM,
    )

    # Define value architecture
    value_mlp = MLP(
        in_features=input_shape[-1],
        activation_class=torch.nn.Tanh,
        out_features=1,
        num_cells=[64, 64],
    )

    # Initialize value weights
    for layer in value_mlp.modules():
        if isinstance(layer, torch.nn.Linear):
            torch.nn.init.orthogonal_(layer.weight, 0.01)
            layer.bias.data.zero_()

    # Define value module
    value_module = ValueOperator(
        value_mlp,
        in_keys=["observation"],
    )

    return policy_module, value_module


def make_ppo_models():
    '''keep this one simple, no common module needed'''

    proof_environment = make_env(batch_size=[1], worker_threads=1, device="cpu")
    actor, critic = make_ppo_models_state(proof_environment)
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
    return torch.cat(test_rewards, 0)


def render_rollout(actor, env, steps, camera='side_camera'): # dm control calling camera
    rollout = env.rollout(
            policy=actor,
            auto_reset=True,
            auto_cast_to_device=True,
            break_when_any_done=False,
            max_steps=steps,
        )
    model = env._mj_model
    data = mujoco.MjData(model)
    mujoco.mj_resetData(model, data)
    model.vis.global_.offheight = 240*2
    model.vis.global_.offwidth = 320*2
    env_id = 0
    all_imgs = []
    with mujoco.Renderer(model, 240*2, 320*2) as rend:
        for t in range(steps):
            state = rollout["observation"][env_id, t].cpu().numpy().astype(np.float64)
            mujoco.mj_setState(model, data, state, mujoco.mjtState.mjSTATE_FULLPHYSICS)
            mujoco.mj_forward(model, data)
            rend.update_scene(data, camera=camera)
            all_imgs.append(rend.render())
    clip = moviepy.editor.ImageSequenceClip(list(all_imgs), fps=50)
    clip.write_videofile("/tmp/rendered_video.mp4", fps=50)
    return "/tmp/rendered_video.mp4"
    