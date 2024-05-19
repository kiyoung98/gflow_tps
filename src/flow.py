import torch
import proxy
from tqdm import tqdm
import openmm.unit as unit
from utils.utils import pairwise_dist 
from torch.distributions import Normal

class FlowNetAgent:
    def __init__(self, args, md):
        self.num_particles = md.num_particles
        self.v_scale = torch.tensor(md.v_scale, dtype=torch.float, device=args.device)
        self.f_scale = torch.tensor(md.f_scale.value_in_unit(unit.femtosecond), dtype=torch.float, device=args.device)
        self.std = torch.tensor(md.std.value_in_unit(unit.nanometer/unit.femtosecond), dtype=torch.float, device=args.device)
        self.masses = torch.tensor(md.masses.value_in_unit(md.masses.unit), dtype=torch.float, device=args.device).unsqueeze(-1)
        self.policy = getattr(proxy, args.molecule.title())(args, md)
        self.normal = Normal(0, self.std)

        if args.train:
            self.replay = ReplayBuffer(args, md)

    def sample(self, args, mds, temperature):
        positions = torch.zeros((args.num_samples, args.num_steps+1, self.num_particles, 3), device=args.device)
        actions = torch.zeros((args.num_samples, args.num_steps, self.num_particles, 3), device=args.device)
        noises = torch.zeros((args.num_samples, args.num_steps, self.num_particles, 3), device=args.device)
        potentials = torch.zeros(args.num_samples, args.num_steps+1, device=args.device)
        
        position, _, _, potential = mds.report()
        
        positions[:, 0] = position
        potentials[:, 0] = potential

        mds.set_temperature(temperature)
        for s in tqdm(range(args.num_steps), desc='Sampling'):
            bias = args.bias_scale * self.policy(position.detach()).squeeze().detach()
            mds.step(bias)
            
            next_position, velocity, force, potential = mds.report()

            # extract noise
            noise = (next_position - position) - (self.v_scale * velocity + self.f_scale * force / self.masses)

            positions[:, s+1] = next_position
            potentials[:, s+1] = potential - (bias*next_position).sum((1, 2)) # Subtract bias potential to get true potential

            position = next_position
            bias = 1e-6 * bias # kJ/(mol*nm) -> (da*nm)/fs**2
            action = self.f_scale * bias / self.masses + noise
            
            actions[:, s] = action
            noises[:, s] = noise
        mds.reset()

        log_md_reward = self.normal.log_prob(actions).mean((1, 2, 3))
        
        target_pd = pairwise_dist(mds.target_position)

        log_target_reward = torch.zeros(args.num_samples, args.num_steps+1, device=args.device)
        for i in range(args.num_samples):
            pd = pairwise_dist(positions[i])
            log_target_reward[i] = - torch.square((pd-target_pd)/args.sigma).mean((1, 2))
        log_target_reward, last_idx = log_target_reward.max(1)
        
        log_reward = log_md_reward + log_target_reward

        if args.train:
            self.replay.add((positions, actions, log_reward))
        
        log = {
            'last_idx': last_idx,
            'positions': positions, 
            'potentials': potentials,
            'log_likelihood': log_md_reward,
            'target_position': mds.target_position,
            'last_position': positions[torch.arange(args.num_samples), last_idx],
            'biased_log_likelihood': self.normal.log_prob(noises).mean((1, 2, 3)),
        }
        return log

    def train(self, args):
        log_z_optimizer = torch.optim.Adam([self.policy.log_z], lr=args.log_z_lr)
        mlp_optimizer = torch.optim.Adam(self.policy.mlp.parameters(), lr=args.mlp_lr)

        positions, actions, log_reward = self.replay.sample()

        biases = args.bias_scale * self.policy(positions[:, :-1])
        biases = 1e-6 * biases # kJ/(mol*nm) -> (da*nm)/fs**2
        biases = self.f_scale * biases / self.masses
        
        log_z = self.policy.log_z
        log_forward = self.normal.log_prob(biases-actions).mean((1, 2, 3))
        loss = (log_z+log_forward-log_reward).square().mean() 
        
        loss.backward()
        
        torch.nn.utils.clip_grad_norm_(self.policy.log_z, args.max_grad_norm)
        torch.nn.utils.clip_grad_norm_(self.policy.mlp.parameters(), args.max_grad_norm)
        
        mlp_optimizer.step()
        log_z_optimizer.step()
        mlp_optimizer.zero_grad()
        log_z_optimizer.zero_grad()
        return loss.item()

class ReplayBuffer:
    def __init__(self, args, md):
        self.positions = torch.zeros((args.buffer_size, args.num_steps+1, md.num_particles, 3), device=args.device)
        self.actions = torch.zeros((args.buffer_size, args.num_steps, md.num_particles, 3), device=args.device)
        self.log_reward = torch.zeros(args.buffer_size, device=args.device)

        self.idx = 0
        self.buffer_size = args.buffer_size
        self.num_samples = args.num_samples

    def add(self, data):
        indices = torch.arange(self.idx, self.idx+self.num_samples) % self.buffer_size
        self.idx += self.num_samples

        self.positions[indices], self.actions[indices], self.log_reward[indices] = data
        
    def sample(self):
        indices = torch.randperm(min(self.idx, self.buffer_size))[:self.num_samples]
        return self.positions[indices], self.actions[indices], self.log_reward[indices]