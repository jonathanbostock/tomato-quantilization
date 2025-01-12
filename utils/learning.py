import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import deque
import random
import logging
from typing import Literal

logger = logging.getLogger(__name__)

class QNetwork(nn.Module):

    def __init__(self, input_channels: int, action_size: int):
        """
        Convolutional Q Network for the tomato gridworld
        
        """
        super().__init__()

        self.conv1 = nn.Conv2d(input_channels, 32, kernel_size=3, stride=1)
        self.bn1 = nn.BatchNorm2d(32)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=3, stride=1)
        self.bn2 = nn.BatchNorm2d(64)
        self.fc1 = nn.Linear(64, 512)
        self.fc2 = nn.Linear(512, action_size)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass of the Q Network

        Args:
            x (torch.Tensor): Input tensor of shape (batch_size, input_channels, height, width)

        Returns:
            torch.Tensor: Output tensor of shape (batch_size, action_size)
        """
        x = F.relu(self.bn1(self.conv1(x)))
        x = F.relu(self.bn2(self.conv2(x)))
        x = x.mean(dim=(-2,-1))
        x = F.relu(self.fc1(x))
        return self.fc2(x)
    
class QAgent:
    def __init__(
            self, *,
            input_channels: int = 6,
            action_size: int = 5,
            gamma: float = 0.99,
            beta_train: float|None = None,
            beta_sample: float|None = None,
            beta_deploy: float|None = None,
            reward_cap: float|None = None):

        self.input_channels = input_channels
        self.action_size = action_size

        self.network = QNetwork(input_channels, action_size)

        self.gamma = gamma
        self.beta_train = beta_train
        self.beta_sample = beta_sample
        self.beta_deploy = beta_deploy

        self.q_cap = None if reward_cap is None else (1/(1-gamma)) * reward_cap

        # Add target network for stable learning
        self.target_network = QNetwork(input_channels, action_size)
        self.target_network.load_state_dict(self.network.state_dict())

    def get_loss(self, data: dict[str, torch.Tensor]):
        """
        Get the loss from a batch of data, containing rewards per state, actions taken, and input states

        Args:
            data (dict[str, torch.Tensor]): Dictionary containing "state", "next_state", "reward", "action", and "action_validity"
            beta (float): Beta value for the temperature scaling

        Returns:
            dict[str, torch.Tensor]: Dictionary containing "loss", "outputs", "probabilities", and "kl_divergence"
        """

        # Predict the rewards for each state, given the beta value
        # This means taking the probabilities of each action and multiplying them by the predicted rewards (Q values)
        # Throw away the first state as we don't know what came before it
        invalid_actions_mask = (~data["action_validity"]).float() * -1e9
        next_state_invalid_actions_mask = (~data["next_state_action_validity"]).float() * -1e9
        outputs = self.network(data["state"]) + invalid_actions_mask

        probabilities = self.beta_softmax(outputs, mode="sample", action_validity=data["action_validity"])
        
        # Use target network with temperature scaling
        with torch.no_grad():
            next_q_values = self.target_network(data["next_state"]) + next_state_invalid_actions_mask
            if self.q_cap is not None:
                next_q_values_capped = next_q_values.clamp(max=self.q_cap)
            else:
                next_q_values_capped = next_q_values

            next_probabilities = self.beta_softmax(
                next_q_values,
                mode="train",
                action_validity=data["action_validity"])
            
            next_values = torch.einsum("ba,ba->b", next_probabilities, next_q_values_capped)

            target_rewards = data["reward"] + self.gamma * next_values

        predicted_rewards = outputs.gather(1, data["action"].unsqueeze(1)).squeeze()
        loss = F.smooth_l1_loss(predicted_rewards, target_rewards)

        # Calculate KL divergence
        base_probabilities = F.softmax(invalid_actions_mask, dim=1)
        probabilities_ratio = base_probabilities / (probabilities + 1e-9) + 1e-9
        kl_divergence = torch.sum(base_probabilities * torch.log(probabilities_ratio), dim=-1).mean()

        return {
            "loss": loss,
            "outputs": outputs,
            "probabilities": probabilities,
            "kl_divergence": kl_divergence,
        }
    
    def get_action(
            self, *,
            state: torch.Tensor,
            action_validity: torch.Tensor|None = None,
            mode: Literal["sample", "deploy"] = "sample") -> int:
        """
        Get the action to take from the network

        Args:
            state (torch.Tensor): Input state of shape (input_channels, height, width)
            action_validity (torch.Tensor|None): Action validity mask of shape (action_size)

        Returns:
            int: Action to take
        """
        outputs = self.network(state)
        probabilities = self.beta_softmax(outputs, mode=mode, action_validity=action_validity)
        # Choose randomly

        try:
            output =  torch.multinomial(probabilities, 1)
        except Exception as e:
            logger.warning(f"Error getting action: {e}")
            logger.warning(f"Probabilities: {probabilities}")
            output = torch.argmax(probabilities, dim=-1)

        if output.shape[0] == 1:
            return output.item()
        else:
            return output.flatten().tolist()
    
    def update_target_network(self, tau: float):
        """
        Update the target network with exponential moving average, using the current network's parameters

        Args:
            tau (float): Tau value for the exponential moving average
        """

        new_state_dict = self.network.state_dict()
        for name, param in new_state_dict.items():
            self.target_network.state_dict()[name].copy_(
                self.target_network.state_dict()[name] * (1-tau) + param * tau)
        
    def beta_softmax(self, outputs: torch.Tensor, mode: Literal["sample", "train", "deploy"], action_validity: torch.Tensor|None = None):
        """
        Softmax with beta scaling

        Args:
            outputs (torch.Tensor): Outputs from the network of shape (batch_size, action_size)
            mode (Literal["sample", "train", "deploy"]): Mode to use for the softmax
            action_validity (torch.Tensor|None): Action validity mask of shape (batch_size, action_size)

        Returns:
            torch.Tensor: Probabilities of shape (batch_size, action_size)
        """

        # Mask out invalid actions
        if mode == "sample":
            beta = self.beta_sample
        elif mode == "train":
            beta = self.beta_train
        elif mode == "deploy":
            beta = self.beta_deploy

        if action_validity is not None:
            outputs = outputs + (~action_validity).float() * -1e9

        # Apply temperature scaling if beta is provided
        if beta is not None:
            probabilities = F.softmax(outputs * beta, dim=-1)
        else:
            probabilities = torch.zeros_like(outputs)
            probabilities[torch.arange(outputs.shape[0]), torch.argmax(outputs, dim=-1)] = 1

        # Clamp negative probabilities
        if probabilities.max() < 0:
            logger.warning("Negative probability detected")
            probabilities = probabilities.clamp(min=0)

        # Clamp NaN or inf probabilities
        if probabilities.isnan().any() or probabilities.isinf().any():
            logger.warning("NaN or inf probability detected")
            probabilities = torch.ones_like(probabilities)


        return probabilities

class StateBuffer:
    def __init__(self, buffer_size: int, batch_size: int):
        self.buffer_size = buffer_size
        self.batch_size = batch_size

        self.buffers: deque[dict[str, torch.Tensor]] = deque(maxlen=buffer_size)

    def add(self, data: dict[str, torch.Tensor]):
        self.buffers.append(data)

    def get_batch(self) -> dict[str, torch.Tensor]:

        if len(self.buffers) < self.batch_size:
            raise ValueError("Not enough data in buffer to get a batch")
        
        batch = random.sample(self.buffers, self.batch_size)
        # Convert list of dicts to dict of lists
        batch_dict = {}
        for key in batch[0].keys():
            batch_dict[key] = torch.stack([d[key] for d in batch])

        return batch_dict
    
    def __len__(self):
        return len(self.buffers)
    
    def clear(self):
        self.buffers.clear()