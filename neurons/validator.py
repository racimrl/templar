# The MIT License (MIT)
# © 2025 tplr.ai

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

# Standard library
import argparse
import asyncio
import concurrent.futures
import hashlib
import os
import random
import sys
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import StringIO
from time import perf_counter
from types import SimpleNamespace
from typing import Deque, cast

import bittensor as bt
import numpy as np

# Third party
import torch
import torch.distributed as dist
import uvloop
from openskill.models import PlackettLuce
from rich.console import Console
from rich.table import Table
from torch.distributed.checkpoint.state_dict import (
    StateDictOptions,
    get_model_state_dict,
    set_model_state_dict,
)
from torch.distributed.tensor import DTensor as DT
from torch.distributed.tensor import distribute_tensor

import tplr
from neurons import BaseNode, Trainer
from neurons.base_node import CPU_COUNT
from tplr.distributed import dist_helper

# GPU optimizations.
torch.manual_seed(42)
torch.cuda.manual_seed_all(42)
np.random.seed(42)
random.seed(42)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True


class NullMetricsLogger:
    def log(self, *_args, **_kwargs) -> None:
        return


@contextmanager
def timer(name: str, wandb_obj=None, step=None, metrics_logger=None):
    start = perf_counter()
    yield
    duration = perf_counter() - start
    tplr.logger.debug(f"{name} took {duration:.2f}s")
    if wandb_obj and step is not None:
        wandb_obj.log({f"validator/{name}": duration}, step=step)
    if metrics_logger and step is not None:
        metrics_logger.log(
            measurement="timing", tags={"window": step}, fields={name: duration}
        )


class Validator(BaseNode, Trainer):
    @staticmethod
    def validator_config():
        parser = argparse.ArgumentParser(description="Validator script")
        parser.add_argument(
            "--netuid", type=int, default=268, help="Bittensor network UID."
        )
        parser.add_argument(
            "--project", type=str, default="templar", help="Wandb project."
        )
        parser.add_argument(
            "--device", type=str, default="cuda", help="Device to use for training"
        )
        parser.add_argument("--debug", action="store_true", help="Enable debug logging")
        parser.add_argument("--trace", action="store_true", help="Enable trace logging")
        parser.add_argument(
            "--store-gathers",
            action="store_true",
            help="Store gathered gradients in R2",
        )
        parser.add_argument(
            "--test",
            action="store_true",
            help="Test mode - use all peers without filtering",
        )
        parser.add_argument(
            "--local",
            action="store_true",
            help="Local run - use toy model, small enough for a laptop.",
        )
        parser.add_argument(
            "--profile-iters",
            type=int,
            default=0,
            help="Active iterations per Torch‑Profiler trace (0 = disable)",
        )
        parser.add_argument(
            "--profile-dir",
            type=str,
            default="./log/profiler",
            help="Directory to save profiler traces",
        )

        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        config = bt.config(parser)
        if config.debug:
            tplr.debug()
        if config.trace:
            tplr.trace()

        return config

    def _save_model_state(self):
        """Save model state efficiently using torch distributed checkpoint utilities.
        Returns state dict with tensors offloaded to CPU.
        """
        # Get model state dict with local shards only, already on CPU
        # cpu_offload=True means tensors are moved to CPU
        # full_state_dict=False means we only save local DTensor shards
        state_dict = get_model_state_dict(
            self.model,
            options=StateDictOptions(
                full_state_dict=False,  # Only save local shards, not full model
                cpu_offload=True,  # Automatically offload to CPU
            ),
        )
        return state_dict

    def _restore_model_state(self, state_dict):
        """Restore model state from saved state dict.
        Handles DTensor and regular tensors efficiently.
        """
        # set_model_state_dict handles moving from CPU to device and DTensor reconstruction
        set_model_state_dict(
            self.model,
            dict(state_dict),
            options=StateDictOptions(
                full_state_dict=False,  # We saved only local shards, so load them back as such
                strict=True,  # Ensure all keys match
            ),
        )

    def __init__(self):
        tplr.logger.debug("Starting initialization...")

        # Init config and load hparams
        self.config = Validator.validator_config()

        # ────────────────────────────────────────────────────────────────
        # Distributed initialisation ─ exactly the same pattern as *miner*
        # ────────────────────────────────────────────────────────────────
        backend = "nccl" if torch.cuda.is_available() else "gloo"
        dist_helper.init_process_group(backend=backend, timeout_minutes=30)
        self.rank = dist_helper.rank
        self.world_size = dist_helper.world_size
        self.local_rank = dist_helper.local_rank
        self.is_master = dist_helper.is_master

        if dist_helper.device:
            self.config.device = str(dist_helper.device)
            self.device = dist_helper.device
        else:
            self.config.device = self.config.device or "cuda"
            self.device = torch.device(self.config.device)

        self.hparams = tplr.load_hparams(
            use_local_run_hparams=cast(bool, self.config.local)
        )

        # Init bittensor objects
        self.wallet = bt.wallet(config=self.config)
        super().__init__()

        # Init comms
        self.comms = tplr.comms.Comms(
            wallet=self.wallet,
            save_location="/tmp",
            key_prefix="model",
            config=self.config,
            hparams=self.hparams,
            uid=None,  # UID will be set after comms is initialized
        )

        self.current_hotkeys = dict(
            zip(self.comms.metagraph.uids, self.comms.metagraph.hotkeys)
        )
        if self.wallet.hotkey.ss58_address not in self.comms.metagraph.hotkeys:
            tplr.logger.error(
                f"\n\t[bold]The wallet {self.wallet} is not registered on subnet: {self.comms.metagraph.netuid}[/bold]"
            )
            sys.exit()
        self.uid = self.comms.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
        self.comms.uid = self.uid

        try:
            version = tplr.__version__
            tplr.logger = tplr.setup_loki_logger(
                service="validator", uid=str(self.uid), version=version
            )
            tplr.logger.info(f"Loki logging enabled for validator UID: {self.uid}")
        except Exception as e:
            tplr.logger.warning(f"Failed to initialize Loki logging: {e}")

        self.device = torch.device(self.config.device)
        tplr.logger.info(f"[Init] device set → {self.device}")

        self.init_model(validator=True)
        self.ckpt = tplr.DCPCheckpointer(
            self.comms, uid=self.uid, version=tplr.__version__
        )
        self.expected_compressed_params = self.get_expected_params()
        self.tokenizer = self.hparams.tokenizer

        # Init compression
        self.transformer = tplr.compress.ChunkingTransformer(
            self.model, target_chunk=self.hparams.target_chunk
        )
        self.compressor = tplr.compress.TopKCompressor(
            use_quantization=True,
            quantization_bins=self.hparams.quantization_bins,
            quantization_range=self.hparams.quantization_range,
        )

        # Init optimizer
        self.init_optimizers_schedulers(validator=True)

        self.xshapes = {}
        self.totalks = {}
        # Use bare_model like the miner does to ensure consistent parameter iteration
        for n, p in self.model.named_parameters():
            # Use the same approach as miner for creating xshapes and totalks
            enc = self.transformer.encode(
                torch.empty(p.shape, dtype=torch.float16, device=self.device),
                use_dct=self.hparams.use_dct,
            )
            _, _, xshape, totalk, _ = self.compressor.compress(
                enc,
                self.hparams.topk_compression,
            )
            self.xshapes[n] = xshape
            self.totalks[n] = totalk

        self.openskill_model = PlackettLuce(
            beta=self.hparams.openskill_beta, tau=self.hparams.openskill_tau
        )
        self.openskill_ratings = {}  # Dictionary to store peer ratings

        self.bootstrap_version = getattr(self.hparams, "checkpoint_init_version", None)
        tplr.logger.info(
            f"[Miner] code_version={tplr.__version__} "
            f"checkpoint_init_flag={self.bootstrap_version or '<none>'}"
        )

        self.bucket = self.comms.get_own_bucket("gradients", "read")
        self.comms.try_commit(self.wallet, self.bucket)
        # self.comms.fetch_commitments()

        # Init state params
        self.current_block = self.comms.subtensor.block
        self.current_window = int(self.current_block / self.hparams.blocks_per_window)
        self.start_window = self.current_window  # Record the start window
        self.global_step = 0  # Initialize global_step to zero
        self.comms.current_window = self.current_window
        self.sync_window = self.current_window - self.hparams.validator_offset

        # Init score tracking variables
        self.loss_before_per_batch_own = 0.0
        self.loss_after_per_batch_own = 0.0
        self.loss_before_per_batch_random = 0.0
        self.loss_after_per_batch_random = 0.0
        self.loss_improvement_own = 0.0
        self.loss_improvement_random = 0.0
        self.relative_improvement_own = 0.0
        self.relative_improvement_random = 0.0

        # For better looking graphs if no eval peers could be evaluated
        self.previous_avg_loss_before_own = 0.0
        self.previous_avg_loss_after_own = 0.0
        self.previous_avg_loss_before_random = 0.0
        self.previous_avg_loss_after_random = 0.0
        self.valid_score_indices = []

        # Caching
        self.state_path = f"validator-state-{tplr.__version__}.pt"
        if os.path.isfile(self.state_path):
            self.load_state()
        else:
            d = self.device
            self.gradient_scores = torch.zeros(256, dtype=torch.float32, device=d)
            self.sync_scores = torch.zeros(256, dtype=torch.float32, device=d)
            self.binary_indicator_scores = torch.zeros(
                256, dtype=torch.float32, device=d
            )
            self.final_scores = torch.zeros(256, dtype=torch.float32, device=d)
            self.binary_moving_averages = torch.zeros(
                256, dtype=torch.float32, device=d
            )
            self.weights = torch.zeros(256, dtype=torch.float32, device=d)
        self.evaluated_uids = set()

        # Add step tracking
        self.window_step = 0
        self.eval_count = 0

        # Initialize WandB and metrics logger only on the master rank
        if self.is_master:
            self.wandb = tplr.initialize_wandb(
                run_prefix="V",
                uid=self.uid,
                config=self.config,
                group="validator",
                job_type="validation",
            )

            self.metrics_logger = tplr.metrics.MetricsLogger(
                prefix="V",
                uid=self.uid,
                config=self.config,
                role="validator",
                group="validator",
                job_type="validation",
            )
        else:
            self.wandb = NullMetricsLogger()
            self.metrics_logger = NullMetricsLogger()

        # Weighted selection counters for fair picking of eval peers
        self.eval_peers = defaultdict(lambda: 1)

        # Track inactive peer scores
        self.inactive_scores = {}  # {uid: (last_active_window, last_score)}
        self.inactivity_slash_rate = 0.25  # 25% slash per window
        self.missing_gradient_slash_rate = 0.75
        self.sync_score_slash_rate = 0.75
        self.idx_similarity_slashing_rate = (
            tplr.neurons.instantiate_slashing_multiplier()
        )
        self.naughty_peers = {}
        self.naughty_peer_timeout = 200

        # Initialize peer related attributes
        self.next_peers: list[int] | None = None
        self.next_reserve_peers: list[int] | None = None
        self.peers_update_window = -1

        self.peers_last_eval_window = {}
        self.param_avg_change: dict[str, torch.Tensor] = {}
        self.prev_param_state: dict[str, torch.Tensor] = {}
        self.param_change_alpha = 0.2

        self.windows_per_shard = getattr(self.hparams, "windows_per_shard")
        self.dataset_manager = tplr.sharded_dataset.ShardedDatasetManager(
            sequence_length=self.hparams.sequence_length,
            rank=self.rank,
            world_size=self.world_size,
            comms=self.comms,
        )

        self.burn_uid = 1

        # Track negative evaluation history for each peer (last 20 evaluations)
        self.peer_eval_history: dict[int, Deque[bool]] = {}
        self.eval_history_limit = 20  # Track last 20 evaluations per peer

    def reset_peer(self, uid: int) -> None:
        """
        Generally based on peer behavior, reset their scores

        Args:
            uid: The uid from the chain representing a user
        """
        self.final_scores[uid] = 0.0
        self.weights[uid] = 0.0
        self.gradient_scores[uid] = 0.0
        self.binary_moving_averages[uid] = 0.0
        self.binary_indicator_scores[uid] = 0.0
        self.sync_scores[uid] = 0.0
        self.openskill_ratings.pop(uid, None)
        self.eval_peers.pop(uid, None)
        self.inactive_scores.pop(uid, None)
        self.peer_eval_history.pop(uid, None)
        return

    def track_negative_evaluation(self, eval_uid: int) -> None:
        """
        Track negative evaluation history for a peer over the last N evaluations.
        Stores True for negative, False for positive. Frequency = mean(history).
        """
        # --- get score safely
        is_negative = bool(self.gradient_scores[eval_uid] < 0)

        # --- init on first use
        if eval_uid not in self.peer_eval_history:
            self.peer_eval_history[eval_uid] = deque(maxlen=self.eval_history_limit)

        window = self.peer_eval_history[eval_uid]

        # --- update window
        window.append(is_negative)

        # --- compute stats
        total = len(window)
        neg_count = sum(window)  # True==1, False==0
        negative_freq = (neg_count / total) if total else 0.0

        # --- logging (consider rate limiting in practice)
        tplr.log_with_context(
            level="info",
            message=(
                f"UID {eval_uid} negative eval frequency: "
                f"{neg_count}/{total} ({negative_freq:.1%}) in last {total} evals"
            ),
            sync_window=self.sync_window,
            current_window=self.current_window,
            eval_uid=eval_uid,
        )

        self.wandb.log(
            {
                f"validator/negative_eval/count/{eval_uid}": neg_count,
                f"validator/negative_eval/frequency/{eval_uid}": negative_freq,
                f"validator/negative_eval/total_evals/{eval_uid}": total,
            },
            step=self.global_step,
        )

    def log_sync_score(
        self, eval_uid: int, sync_result: dict[str, bool | float | int | str]
    ) -> None:
        l2_norm = float(sync_result.get("l2_norm", 99.0))
        avg_l2_norm = float(sync_result.get("avg_l2_norm", 99.0))
        avg_abs_diff = float(sync_result.get("avg_abs_diff", 99.0))
        max_diff = float(sync_result.get("max_diff", 99.0))
        avg_steps_behind = float(sync_result.get("avg_steps_behind", 99.0))
        max_steps_behind = float(sync_result.get("max_steps_behind", 99.0))
        tplr.log_with_context(
            level="info",
            message=f"Sync average steps behind: {avg_steps_behind:.3f}",
            sync_window=self.sync_window,
            current_window=self.current_window,
            eval_uid=eval_uid,
        )
        if self.is_master:
            self.wandb.log(
                {
                    f"validator/sync/l2_norm/{eval_uid}": l2_norm,
                    f"validator/sync/avg_l2_norm/{eval_uid}": avg_l2_norm,
                    f"validator/sync/avg_abs_diff/{eval_uid}": avg_abs_diff,
                    f"validator/sync/sync_max_diff/{eval_uid}": max_diff,
                    f"validator/sync/avg_steps_behind/{eval_uid}": avg_steps_behind,
                    f"validator/sync/max_steps_behind/{eval_uid}": max_steps_behind,
                },
                step=self.global_step,
            )
            self.metrics_logger.log(
                measurement="validator_sync_score",
                tags={
                    "uid": str(eval_uid),
                    "window": int(self.sync_window),
                    "global_step": int(self.global_step),
                },
                fields={
                    "l2_norm": l2_norm,
                    "avg_l2_norm": avg_l2_norm,
                    "avg_abs_diff": avg_abs_diff,
                    "max_diff": max_diff,
                    "avg_steps_behind": avg_steps_behind,
                    "max_steps_behind": max_steps_behind,
                },
                with_system_metrics=True,
                with_gpu_metrics=True,
            )

    def update_openskill_ratings(self) -> None:
        """
        Update OpenSkill ratings based on gradient scores and recalculate final scores.

        This method:
        1. Processes all peers evaluated in the current window
        2. Updates their OpenSkill ratings based on gradient performance
        3. Recalculates final scores using OpenSkill mu value combined with binary and sync scores
        4. Logs the updated ratings to monitoring systems

        The OpenSkill rating system provides a probabilistic skill rating that accounts for
        uncertainty and relative performance between peers. Ratings are updated using the
        PlackettLuce model where higher gradient scores indicate better performance.

        The final score calculation combines:
        - OpenSkill mu (mean skill estimate)
        - Binary moving average (filtered to non-negative values)
        - Sync score (model synchronization quality)
        """
        if (
            hasattr(self, "current_window_scores")
            and len(self.current_window_scores) > 1
        ):
            # Get UIDs and scores
            window_uids = list(self.current_window_scores.keys())

            # Store original ordinal values to calculate diff after update
            original_ordinals = {}
            for uid in window_uids:
                if uid in self.openskill_ratings:
                    original_ordinals[uid] = float(
                        self.openskill_ratings[uid].ordinal()
                    )
                else:
                    # For new peers without previous ratings
                    original_ordinals[uid] = 0.0

            # Calculate ranks based on gradient scores (lower rank = better performance)
            # In OpenSkill, ranks start at 1 (best) and increase for worse performers
            scores = [self.current_window_scores[uid] for uid in window_uids]

            # Create teams list for OpenSkill
            teams = [[self.openskill_ratings[uid]] for uid in window_uids]

            # Rate the teams using scores (higher score is better in OpenSkill)
            rated_teams = self.openskill_model.rate(teams, scores=scores)

            # Store updated ratings
            for i, uid in enumerate(window_uids):
                self.openskill_ratings[uid] = rated_teams[i][0]

                # Log updated OpenSkill values
                openskill_mu = float(self.openskill_ratings[uid].mu)
                openskill_sigma = float(self.openskill_ratings[uid].sigma)
                openskill_ordinal = float(self.openskill_ratings[uid].ordinal())

                sync_score = float(
                    self.sync_scores[uid].item() if uid in self.evaluated_uids else 0.0
                )

                self.final_scores[uid] = (
                    openskill_ordinal
                    * max(0, self.binary_moving_averages[uid].item())
                    * sync_score
                )
                tplr.log_with_context(
                    level="info",
                    message=f"Computed Final Score for UID {uid}: {self.final_scores[uid]}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                    eval_uid=uid,
                )

                # Log to WandB
                if self.is_master:
                    self.wandb.log(
                        {
                            f"validator/openskill/mu/{uid}": openskill_mu,
                            f"validator/openskill/sigma/{uid}": openskill_sigma,
                            f"validator/openskill/ordinal/{uid}": openskill_ordinal,
                        },
                        step=self.global_step,
                    )

                    # Log to InfluxDB
                    self.metrics_logger.log(
                        measurement="validator_openskill",
                        tags={
                            "eval_uid": str(uid),
                            "window": int(self.sync_window),
                            "global_step": int(self.global_step),
                        },
                        fields={
                            "mu": openskill_mu,
                            "sigma": openskill_sigma,
                            "ordinal": openskill_ordinal,
                        },
                    )

            # Create a ranking table to display current match rankings
            try:
                # Sort UIDs by current window gradient scores (descending)
                sorted_uids = sorted(
                    window_uids,
                    key=lambda uid: self.current_window_scores[uid],
                    reverse=True,
                )

                try:
                    width = os.get_terminal_size().columns
                except Exception:
                    width = 0
                os.environ["COLUMNS"] = str(max(200, width))

                rich_table = Table(
                    title=f"Current Match Rankings (Window {self.sync_window})"
                )
                rich_table.add_column("Match Rank")
                rich_table.add_column("UID")
                rich_table.add_column("Match Score")
                rich_table.add_column("OpenSkill μ (After)")
                rich_table.add_column("OpenSkill σ (After)")
                rich_table.add_column("Ordinal (After)")
                rich_table.add_column("Ordinal Δ")

                # Add rows to table
                for rank, uid in enumerate(sorted_uids, 1):
                    rating = self.openskill_ratings[uid]
                    ordinal_before = original_ordinals[uid]
                    ordinal_after = rating.ordinal()
                    ordinal_diff = ordinal_after - ordinal_before

                    # Format the diff with color indicators
                    diff_str = f"{ordinal_diff:+.4f}"

                    rich_table.add_row(
                        str(rank),
                        str(uid),
                        f"{self.current_window_scores[uid]:.6f}",
                        f"{rating.mu:.4f}",
                        f"{rating.sigma:.4f}",
                        f"{ordinal_after:.4f}",
                        diff_str,
                    )

                # Render table to string
                sio = StringIO()
                console = Console(file=sio, width=int(os.environ["COLUMNS"]))
                console.print(rich_table)
                table_str = sio.getvalue()

                tplr.log_with_context(
                    level="info",
                    message=f"Current Match Rankings (Window {self.sync_window}):\n{table_str}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
            except Exception as e:
                tplr.log_with_context(
                    level="warning",
                    message=f"Failed to create OpenSkill rankings table: {str(e)}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            tplr.log_with_context(
                level="info",
                message=f"Updated OpenSkill ratings for {len(window_uids)} peers based on gradient scores",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )

            # Clear the current window scores
            self.current_window_scores = {}

    def update_weights(self) -> None:
        """
        Piece‑wise emissions schedule:

        ┌ Gather peers (top N)      ── 75 % of non‑burned weight
        │   linear ramp: w₁ = 2·wₙ
        ├ Reserve peers (next R)    ── 25 % of non‑burned weight
        │   linear decay: 1 → 0.1
        └ Everyone else             ── 0
        """
        self.weights.zero_()

        # --- configurable knobs (with sane defaults) -------------------
        burn_rate = max(0.0, min(1.0, self.hparams.burn_rate))
        gather_share = getattr(self.hparams, "gather_share", 0.75)
        gather_count = getattr(self.hparams, "gather_peer_count", 15)
        reserve_count = getattr(self.hparams, "reserve_peer_count", 10)
        top_ratio = getattr(self.hparams, "gather_top_ratio", 2.0)  # w₁ / wₙ
        decay_ratio = getattr(self.hparams, "reserve_decay_ratio", 0.75)

        # --- pick peers -------------------------------------------------
        positive_uids = [
            uid for uid in self.evaluated_uids if self.final_scores[uid] > 0
        ]
        ranked = sorted(
            positive_uids, key=lambda u: self.final_scores[u].item(), reverse=True
        )

        gather_uids = ranked[:gather_count]
        reserve_uids = ranked[gather_count : gather_count + reserve_count]

        # --- distribute to gather peers ---------------------------------
        if gather_uids:
            num_gather = len(gather_uids)
            gather_profile = torch.linspace(
                top_ratio,
                1.0,
                num_gather,
                device=self.weights.device,
                dtype=torch.float32,
            )
            gather_profile_sum = gather_profile.sum()
            gather_total = (1.0 - burn_rate) * gather_share
            for uid, num_reserve in zip(gather_uids, gather_profile):
                self.weights[uid] = gather_total * (num_reserve / gather_profile_sum)

        # --- distribute to reserve peers (decaying schedule) ------------
        if reserve_uids:
            num_reserve = len(reserve_uids)
            # geometric sequence: 1, k, k², …, k^{r‑1}
            reserve_profile = torch.tensor(
                [decay_ratio**i for i in range(num_reserve)],
                device=self.weights.device,
                dtype=torch.float32,
            )
            reserve_profile_sum = reserve_profile.sum()
            reserve_total = (1.0 - burn_rate) * (1.0 - gather_share)
            for uid, rv in zip(reserve_uids, reserve_profile):
                self.weights[uid] = reserve_total * (rv / reserve_profile_sum)

        # ── guard: cap reserve so it never exceeds min‑gather ──────────
        if gather_uids and reserve_uids:
            min_gather = self.weights[gather_uids].min().item()
            max_reserve = self.weights[reserve_uids].max().item()
            if max_reserve > min_gather:  # need down‑scaling
                factor = min_gather / max_reserve * decay_ratio
                self.weights[reserve_uids] *= factor

        # --- burn weight -------------------------------------------------
        self.weights[self.burn_uid] = burn_rate

        # sum of the non‑burn weights currently assigned
        non_burn_sum = self.weights.sum() - burn_rate

        if non_burn_sum > 0:
            scale = (1.0 - burn_rate) / non_burn_sum
            self.weights *= scale  # rescale gather+reserve only
            self.weights[self.burn_uid] = burn_rate  # restore exact burn
        else:
            # fall‑back: allocate all non‑burn weight to burn_uid
            self.weights[self.burn_uid] = 1.0

    async def run(self):
        # Start background block listener
        self.loop = asyncio.get_running_loop()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=CPU_COUNT)
        self.loop.set_default_executor(self.executor)

        # Use config peers if provided
        if self.config.peers:
            self.comms.peers = self.config.peers

        # Only master fetches commitments and updates peers
        if self.is_master:
            self.comms.commitments = await self.comms.get_commitments()
            self.comms.update_peers_with_buckets()
            tplr.logger.info("Loaded commitments")
        else:
            self.comms.commitments = {}

        # Handle start_window similar to miner - only master rank checks and posts
        if self.is_master:
            # Only post start window if you are the highest stake validator
            if self.uid == self.comms.metagraph.S.argmax().item():
                # Check if an existing start window already exists
                try:
                    existing_start_window = await self.comms.get_start_window(retries=2)
                except Exception as e:
                    tplr.logger.warning(f"Error fetching existing start_window: {e}")
                    existing_start_window = None

                if existing_start_window is not None:
                    self.start_window = existing_start_window
                    tplr.logger.info(
                        f"Highest staked validator found existing start_window: {self.start_window}"
                    )
                else:
                    # No existing start window, so post new start window to R2
                    await self.comms.post_start_window(cast(int, self.start_window))
                    tplr.logger.info(
                        f"This validator is the highest staked. Posted start_window: {self.start_window}"
                    )
            else:
                tplr.logger.info(
                    "This validator is not the highest staked. Waiting to fetch start_window."
                )
                self.start_window = await self.comms.get_start_window()

            # Broadcast start_window to all ranks if distributed
            val = -1 if self.start_window is None else self.start_window
            tensor = torch.tensor([val], dtype=torch.long, device=self.device)
            dist_helper.broadcast(tensor, src=0)
        else:
            # Non-master ranks receive start_window via broadcast
            tensor = torch.tensor([0], dtype=torch.long, device=self.device)
            dist_helper.broadcast(tensor, src=0)
            val = tensor.item()
            self.start_window = None if val == -1 else int(val)

        if self.start_window is None:
            raise RuntimeError(
                "Could not find a valid start window. This should not be possible."
            )

        self.global_step = self.current_window - self.start_window
        tplr.logger.info(
            f"Using start_window: {self.start_window}, global_step: {self.global_step}"
        )

        current_shard = self.global_step // self.windows_per_shard
        _ = await self.dataset_manager.initialize_datasets(current_shard)
        self.set_dataloader(validator=True)

        # Load the most recent checkpoint
        _ = await self.load_checkpoint()

        if self.is_master:
            self.comms.start_commitment_fetcher()
            self.comms.start_background_tasks()
        ts_value = 0.0
        time_min = None
        self.last_peer_update_window = None
        self.last_peer_post_window = None
        while not self.stop_event.is_set():
            # 1. Wait until the chain has moved `validator_offset` windows ahead
            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message=(
                        f"Waiting for validator window offset "
                        f"(sync={self.sync_window}, current={self.current_window}, "
                        f"offset={self.hparams.validator_offset})"
                    ),
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            await self.wait_until_window(
                self.sync_window + self.hparams.validator_offset + 1
            )

            # 2. Increment sync window and update peer lists
            window_start = tplr.T()

            if self.global_step > 0 and self.global_step % self.windows_per_shard == 0:
                tplr.logger.info(f"Swapping dataset at window {self.current_window}")
                await self.dataset_manager.swap_datasets()
                self.set_dataloader(validator=True)

            self.sync_window += 1
            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message=f"Sync Window: {self.sync_window}, Global step: {self.global_step}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                tplr.log_with_context(
                    level="info",
                    message=f"Processing window: {self.sync_window} current: {self.current_window}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            # Save state
            await self.save_state()

            # Only master handles peer list fetching and updates
            peer_update_time = 0.0
            if self.is_master:
                # Create and post peers
                _ = await self.create_and_post_peers()

                self.comms.update_peers_with_buckets()
                peer_start = tplr.T()
                await tplr.neurons.update_peers(
                    instance=self, window=self.sync_window, peer_start=peer_start
                )
                peer_update_time = tplr.T() - peer_start

                self.eval_peers = {
                    peer: value
                    for peer, value in self.comms.eval_peers.items()
                    if peer not in self.naughty_peers
                }
            else:
                # Non-master ranks don't need eval_peers
                self.eval_peers = {}

            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message=f"{tplr.P(self.sync_window, peer_update_time)} Updated peers - eval:{len(self.eval_peers)}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                tplr.log_with_context(
                    level="info",
                    message=f"Current gather peers: {self.comms.peers}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                tplr.log_with_context(
                    level="info",
                    message=f"Current evaluation peers: {self.eval_peers}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                newly_inactive = self.comms.inactive_peers
                current_window = self.sync_window

                # 3. Process inactive peers and apply penalties
                for uid in newly_inactive:
                    if uid not in self.inactive_scores:
                        self.inactive_scores[uid] = (
                            current_window,
                            self.final_scores[uid].item(),
                        )
                        tplr.log_with_context(
                            level="info",
                            message=f"UID {uid} became inactive at window {current_window} with score {self.final_scores[uid].item():.4f}",
                            sync_window=self.sync_window,
                            current_window=self.current_window,
                        )

                # Apply penalties to all inactive peers
                _ = self.penalize_inactive_peers()

                sync_block = (self.sync_window + 1) * self.hparams.blocks_per_window
                ts_value = self.query_block_timestamp(sync_block)
                if ts_value is None:
                    tplr.log_with_context(
                        level="warning",
                        message=f"Could not get timestamp for sync block {sync_block}. Using current time as fall back.",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                    )
                    ts_value = time.time()
                time_min = datetime.fromtimestamp(ts_value, tz=timezone.utc)
                time_max = time_min + timedelta(
                    seconds=self.hparams.time_window_delta_seconds
                )

                # Log the time window we're using
                tplr.log_with_context(
                    level="info",
                    message=f"Using time window for gather: {time_min} to {time_max}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                tplr.log_with_context(
                    level="info",
                    message=f"We are using peers {self.comms.peers}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                # Refresh peers explicitly before starting gather to avoid missing updated active peers.
                tplr.log_with_context(
                    level="info",
                    message="Refreshing eval peers before gather task in validator...",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            if self.config.test:
                # In test mode, use all UIDs from metagraph except self
                tplr.log_with_context(
                    level="info",
                    message="Test mode active: Using all peers from metagraph.",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                all_uids = list(range(1, len(self.comms.metagraph.S)))
                self.comms.peers = [uid for uid in all_uids if uid != self.uid]

                # For evaluation, also use all peers but track separately with equal initial weight
                self.eval_peers = {uid: 1 for uid in self.comms.peers}

            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message=f"Validator gather peers: {self.comms.peers}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            gather_start = tplr.T()
            skipped_uids: list[int] = []
            success_rate = 0.0
            gather_result = None
            skip_window = False

            # Only master rank performs the gather operation
            if self.is_master:
                gather_result = await self.comms.gather_with_reserve(
                    my_uid=self.uid,
                    gather_uids=self.comms.peers,
                    reserve_uids=self.comms.reserve_peers,
                    window=self.sync_window,
                    key="gradient",
                    timeout=75,
                    device=cast(str, self.device),
                    local=False,
                    totalks=self.totalks,
                    compressor=self.compressor,
                    time_min=time_min,
                    time_max=time_max,
                    expected_compressed_params=self.expected_compressed_params,
                )

                if gather_result is None:
                    tplr.log_with_context(
                        level="error",
                        message="Failed to gather gradients from peers. Waiting for next window.",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                    )
                    skip_window = True

            # Broadcast decision to skip window from master to all ranks
            skip_tensor = torch.tensor(
                [1 if skip_window else 0], device=self.device, dtype=torch.int32
            )
            dist_helper.broadcast(skip_tensor, src=0)
            skip_window = bool(skip_tensor.item())

            if skip_window:
                self.global_step += 1
                continue

            # --------------------------------------------------------------+
            #  Simulate the miner’s *inner* loop so the LR schedule advances │
            # --------------------------------------------------------------+
            for _ in range(self.hparams.inner_steps):
                self.inner_scheduler.step()

            # current inner‑LR after simulation
            current_inner_lr = self.inner_scheduler.get_last_lr()[0]

            # Only master performs additional gather processing
            if self.is_master and gather_result is not None:
                t = asyncio.create_task(self.upload_gather_results(gather_result))
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)

                overlap_start = time.time()
                idx_overlap = await tplr.neurons.check_uid_index_overlap(
                    self,
                    gather_result,
                    self.sync_window,
                    overlap_threshold=self.hparams.idx_overlap_threshold,
                )
                overlap_time = time.time() - overlap_start
                tplr.log_with_context(
                    level="info",
                    message=f"check_uid_index_overlap completed in {overlap_time:.3f}s | UIDs checked: {len(gather_result.uids)} | Overlaps found: {len(idx_overlap)}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                # Skip overlap slashing for the first 20 global steps
                if self.global_step >= 20:
                    self.slash_from_overlap(idx_overlap)
                else:
                    tplr.log_with_context(
                        level="info",
                        message=f"Skipping overlap slashing at global step {self.global_step} (waiting until step 20)",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                    )

                skipped_uids = gather_result.skipped_uids
                success_rate = gather_result.success_rate

            # Synchronize all ranks after gather processing
            dist_helper.safe_barrier("post_gather", self.local_rank)
            gather_time = tplr.T() - gather_start

            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message=f"Skipped UIDs: {skipped_uids}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            # Compute gather-quality metrics (only on master)
            intended_gather_uids = list(self.comms.peers)
            actual_gather_uids = []
            mean_final_intended = 0.0
            mean_final_actual = 0.0
            reserve_used = 0

            if self.is_master and gather_result is not None:
                actual_gather_uids = list(gather_result.uids)

                mean_final_intended = (
                    float(self.final_scores[intended_gather_uids].mean().item())
                    if intended_gather_uids
                    else 0.0
                )
                mean_final_actual = (
                    float(self.final_scores[actual_gather_uids].mean().item())
                    if actual_gather_uids
                    else 0.0
                )
                reserve_used = len(
                    [
                        uid
                        for uid in actual_gather_uids
                        if uid in self.comms.reserve_peers
                    ]
                )

            # Only master evaluates miner sync and applies slashing
            if self.is_master:
                await self.slash_for_poor_sync()
                self.slash_for_missing_gradients(skipped_uids, success_rate)

            # Add check for empty peers (evaluating all peer uids)
            # Only master checks and broadcasts the decision
            has_peers = True
            if self.is_master:
                has_peers = len(self.comms.eval_peers) > 0
                if not has_peers:
                    tplr.log_with_context(
                        level="warning",
                        message=f"No peers available for evaluation in window {self.sync_window}. Waiting for next window.",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                    )

            # Barrier before evaluation starts
            if self.world_size > 1 and dist.is_initialized():
                dist.barrier()

            # Broadcast the decision to all ranks
            has_peers_tensor = torch.tensor(
                [has_peers], dtype=torch.bool, device=self.device
            )
            dist_helper.broadcast(has_peers_tensor, src=0)
            has_peers = has_peers_tensor.item()

            if not has_peers:
                self.global_step += 1
                continue

            # Barrier before evaluation starts (soft)
            tplr.log_with_context(
                level="info",
                message="Pre-evaluation barrier sync",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            dist_helper.safe_barrier("pre_evaluation", self.local_rank)

            # 5. Save original model state for evaluation
            eval_start = tplr.T()
            eval_window = self.current_window  # Store window at evaluation start

            # 6. Select peers to evaluate using bin rotation (master selects and broadcasts)
            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message="Creating performance bins for peer evaluation",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                # Create performance bins
                performance_bins = self.bin_evaluation_peers(
                    num_bins=self.hparams.num_evaluation_bins
                )

                # Select which bin to evaluate in this window
                current_bin = self.select_next_bin_for_evaluation(
                    num_bins=self.hparams.num_evaluation_bins
                )

                # Select peers from the chosen bin using weighted sampling
                evaluation_uids = self.select_evaluation_uids_from_bin(
                    performance_bins,
                    current_bin,
                )

                tplr.log_with_context(
                    level="info",
                    message=f"Evaluating peers from bin {current_bin}: {evaluation_uids}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                # Calculate norms from gather result
                clip_norm_dict = {}
                if gather_result is not None:
                    clip_norm_dict = self.compute_peer_val_norms(gather_result)
            else:
                # Non-master ranks prepare empty structures
                evaluation_uids = []
                clip_norm_dict = {}

            # Broadcast only evaluation UIDs from master to all ranks
            # Prepare tensors for broadcasting
            if self.is_master:
                # Convert evaluation UIDs to tensor (pad with -1 if needed)
                eval_uids_tensor = torch.tensor(
                    evaluation_uids + [-1] * (256 - len(evaluation_uids)),
                    dtype=torch.int32,
                    device=self.device,
                )
            else:
                eval_uids_tensor = torch.zeros(
                    256, dtype=torch.int32, device=self.device
                )

            # Broadcast from master to all ranks
            dist_helper.broadcast(eval_uids_tensor, src=0)

            # Reconstruct values on non-master ranks
            if not self.is_master:
                evaluation_uids = [
                    int(uid.item()) for uid in eval_uids_tensor if uid >= 0
                ]

            # Each rank generates its own random seed for dataloader
            random_seed = random.randint(1000, 10000000)

            # ── Help pyright: predeclare locals used in arithmetic/logging
            loss_before_random: float
            n_batches: int

            # Loss before random data
            self.sampler.set_window_uid(random_seed, self.sync_window)
            loss_before_random, n_batches = await self.evaluate_model(
                self.model, self.loader
            )
            self.loss_before_per_batch_random = (
                loss_before_random / n_batches if n_batches > 0 else 0
            )

            # Track UIDs that were attempted to be evaluated this window
            uids_attempted_this_window: set[int] = set()

            avg_loss_before_per_batch_own = 0.0
            avg_loss_after_per_batch_own = 0.0
            avg_loss_before_per_batch_random = 0.0
            avg_loss_after_per_batch_random = 0.0
            evaluated_peers = 0

            # Synchronize all ranks before starting evaluation loop
            dist_helper.safe_barrier("pre_eval_loop", self.local_rank)

            # ── Help pyright in the UID loop
            gradient_apply_time: float = 0.0
            restore_time: float = 0.0
            offload_time: float = 0.0
            evaluation_time: float = 0.0
            loss_before_own: float = 0.0
            loss_after_own: float = 0.0
            loss_after_random: float = 0.0
            # Use CPU offloading instead of deepcopy to save memory
            offload_start = tplr.T()
            save_ok_local = True
            try:
                saved_state = self._save_model_state()
            except Exception as e:
                save_ok_local = False
                tplr.log_with_context(
                    level="error",
                    message=f"Model state save failed: {e}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                saved_state = None
            save_ok_global = dist_helper.all_ok(
                save_ok_local, self.device, tag="save_model_state"
            )
            if not save_ok_global:
                # keep all ranks aligned and skip this whole evaluation window
                dist_helper.safe_barrier("bail_after_save_fail", self.local_rank)
                evaluation_time = tplr.T() - eval_start
                dist_helper.safe_barrier("post_eval", self.local_rank)
                continue
            offload_time = tplr.T() - offload_start

            if self.is_master:
                tplr.log_with_context(
                    level="debug",
                    message=f"Model state offloading took {offload_time:.3f}s",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            # Process each UID with sliding window loading
            for eval_uid in evaluation_uids:
                uid_eval_start = time.time()
                # Check if window has changed before starting evaluation
                if self.current_window != eval_window:
                    if self.is_master:
                        tplr.log_with_context(
                            level="info",
                            message=f"Window changed during evaluation (was {eval_window},"
                            f" now {self.current_window}), exiting evaluation loop early."
                            f" Evaluated {len(uids_attempted_this_window)}/{len(evaluation_uids)} UIDs.",
                            sync_window=self.sync_window,
                            current_window=self.current_window,
                        )
                    break

                # Mark this UID as attempted (for counter management)
                uids_attempted_this_window.add(eval_uid)
                self.peers_last_eval_window[eval_uid] = self.sync_window

                if self.is_master:
                    tplr.log_with_context(
                        level="info",
                        message=f"Evaluating UID: {eval_uid}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                # Only master fetches gradient data for evaluation
                eval_result = {}, 0
                state_dict = {}
                gradient_valid = True  # Track if gradient is valid

                if self.is_master:
                    eval_result = await self.comms.get(
                        uid=str(eval_uid),
                        window=self.sync_window,
                        key="gradient",
                        local=False,
                        stale_retention=10,
                        time_max=time_max,
                        time_min=time_min,
                    )
                    if (
                        not eval_result.success
                        or not isinstance(eval_result.data, dict)
                        or eval_result.data.get("__status") in ["TOO_LATE", "TOO_EARLY"]
                    ):
                        # Slash the peer for missing gradient
                        self.slash_for_missing_gradient(eval_uid)
                        gradient_valid = False

                    else:
                        state_dict = eval_result.data
                        try:
                            self.validate_gradient_data(
                                self.model, eval_uid, state_dict
                            )
                            meta = state_dict.get("metadata", {})
                            self.log_digest_match(eval_uid, meta)
                            _, total_samples = self._training_pool_digest(
                                eval_uid, self.sync_window
                            )
                            total_batches = (
                                total_samples // self.hparams.micro_batch_size
                            )
                        except Exception as e:
                            self.slash_for_invalid_gradient(eval_uid, e)
                            gradient_valid = False

                # Broadcast gradient validity to all ranks
                gradient_valid_tensor = torch.tensor(
                    [gradient_valid], dtype=torch.bool, device=self.device
                )
                dist_helper.broadcast(gradient_valid_tensor, src=0)
                gradient_valid = gradient_valid_tensor.item()

                # All ranks skip if gradient is invalid
                if not gradient_valid:
                    continue

                # Synchronize all ranks after gradient validation
                dist_helper.safe_barrier("post_grad_validate", self.local_rank)

                # Loss before own data
                eval_ok_local = True
                try:
                    self.sampler.set_window_uid(eval_uid, self.sync_window)
                    loss_before_own, n_batches = await self.evaluate_model(
                        self.model, self.loader
                    )
                except Exception as e:
                    eval_ok_local = False
                    tplr.log_with_context(
                        level="error",
                        message=f"evaluate_model (own/before) failed for UID {eval_uid}: {e}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )
                eval_ok_global = dist_helper.all_ok(
                    eval_ok_local, self.device, tag=f"own_before_uid_{eval_uid}"
                )
                if not eval_ok_global:
                    # Align and skip this UID
                    dist_helper.safe_barrier(
                        tag=f"skip_uid_own_before_{eval_uid}",
                        local_rank=self.local_rank,
                    )
                    continue
                # (if ok, loss_before_own/n_batches exist)

                # evaluate_model now handles averaging across ranks
                self.loss_before_per_batch_own = (
                    loss_before_own / n_batches if n_batches > 0 else 0
                )
                if self.is_master:
                    tplr.log_with_context(
                        level="info",
                        message=f"Evaluating {n_batches}/{total_batches} batches ({n_batches / total_batches:.1%})",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                    tplr.log_with_context(
                        level="debug",
                        message=f"Loss before (own data): {self.loss_before_per_batch_own}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                    tplr.log_with_context(
                        level="debug",
                        message=f"Loss before (random data): {self.loss_before_per_batch_random}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                # 9. Apply gradient and compute loss after
                apply_ok_local = True
                try:
                    gradient_apply_start = tplr.T()
                    self.update_model_with_gradient(
                        self.model,
                        eval_uid,
                        state_dict,
                        clip_norm_dict,
                    )
                    gradient_apply_time = tplr.T() - gradient_apply_start
                    if self.is_master:
                        tplr.log_with_context(
                            level="debug",
                            message=f"Gradient application took {gradient_apply_time:.3f}s",
                            sync_window=self.sync_window,
                            current_window=self.current_window,
                            eval_uid=eval_uid,
                        )
                except Exception as e:
                    apply_ok_local = False
                    if self.is_master:
                        self.slash_for_invalid_gradient(eval_uid, e)

                # Reach group consensus before any barrier
                apply_ok_global = dist_helper.all_ok(
                    apply_ok_local, self.device, tag=f"apply_grad_uid_{eval_uid}"
                )
                if not apply_ok_global:
                    # Restore and skip in lockstep
                    self._restore_model_state(saved_state)
                    dist_helper.safe_barrier(
                        tag=f"skip_uid_{eval_uid}", local_rank=self.local_rank
                    )
                    continue

                # Synchronize all ranks after gradient application
                dist_helper.safe_barrier("post_apply_grad_uid", self.local_rank)

                # 10. Compute loss after gradient application on own data
                self.outer_optimizer.zero_grad()
                self.model.zero_grad()
                self.sampler.set_window_uid(eval_uid, self.sync_window)
                eval_ok_local = True
                try:
                    loss_after_own, n_batches = await self.evaluate_model(
                        self.model, self.loader
                    )
                except Exception as e:
                    eval_ok_local = False
                    tplr.log_with_context(
                        level="error",
                        message=f"evaluate_model (own/after) failed for UID {eval_uid}: {e}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )
                eval_ok_global = dist_helper.all_ok(
                    eval_ok_local, self.device, tag=f"own_after_uid_{eval_uid}"
                )
                if not eval_ok_global:
                    # We already applied the peer's gradient; restore before skipping.
                    self._restore_model_state(saved_state)
                    dist_helper.safe_barrier(
                        tag=f"skip_uid_own_after_{eval_uid}", local_rank=self.local_rank
                    )
                    continue

                # evaluate_model now handles averaging across ranks
                # Clean up stored batches
                torch.cuda.empty_cache()

                self.loss_after_per_batch_own = (
                    loss_after_own / n_batches if n_batches > 0 else 0
                )
                avg_loss_before_per_batch_own += self.loss_before_per_batch_own
                avg_loss_after_per_batch_own += self.loss_after_per_batch_own
                if self.is_master:
                    tplr.log_with_context(
                        level="debug",
                        message=f"Loss after (own data): {self.loss_after_per_batch_own}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                # 11. Calculate improvements and update scores
                # Compute and assign the loss improvement to self
                self.loss_improvement_own = (
                    self.loss_before_per_batch_own - self.loss_after_per_batch_own
                )
                if self.is_master:
                    tplr.log_with_context(
                        level="debug",
                        message=f"Loss improvement (own data): {self.loss_improvement_own}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                self.relative_improvement_own = (
                    self.loss_improvement_own / self.loss_before_per_batch_own
                    if self.loss_before_per_batch_own > 0
                    else 0.0
                )
                if self.is_master:
                    tplr.log_with_context(
                        level="debug",
                        message=f"Relative improvement (own data): {self.relative_improvement_own:.4f}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                # 10. Compute loss after gradient application for random data
                self.outer_optimizer.zero_grad()
                self.model.zero_grad()

                self.sampler.set_window_uid(random_seed, self.sync_window)
                eval_ok_local = True
                try:
                    loss_after_random, n_batches = await self.evaluate_model(
                        self.model,
                        self.loader,
                    )
                except Exception as e:
                    eval_ok_local = False
                    tplr.log_with_context(
                        level="error",
                        message=f"evaluate_model (random/after) failed for UID {eval_uid}: {e}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )
                eval_ok_global = dist_helper.all_ok(
                    eval_ok_local, self.device, tag=f"random_after_uid_{eval_uid}"
                )
                if not eval_ok_global:
                    self._restore_model_state(saved_state)
                    dist_helper.safe_barrier(
                        tag=f"skip_uid_random_after_{eval_uid}",
                        local_rank=self.local_rank,
                    )
                    continue
                # evaluate_model now handles averaging across ranks

                # Restore original model parameters from CPU
                restore_start: float = tplr.T()
                self._restore_model_state(saved_state)
                restore_time = tplr.T() - restore_start
                # restore_time is now always a float, never Unbound

                if self.is_master:
                    tplr.log_with_context(
                        level="debug",
                        message=f"Model state restoration took {restore_time:.3f}s",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                # Clean up saved state
                torch.cuda.empty_cache()

                self.loss_after_per_batch_random = (
                    loss_after_random / n_batches if n_batches > 0 else 0
                )

                avg_loss_before_per_batch_random += self.loss_before_per_batch_random
                avg_loss_after_per_batch_random += self.loss_after_per_batch_random

                if self.is_master:
                    tplr.log_with_context(
                        level="info",
                        message=f"Loss after (random data): {self.loss_after_per_batch_random}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                # 11. Calculate improvements and update scores
                # Compute and assign the loss improvement to self
                self.loss_improvement_random = (
                    self.loss_before_per_batch_random - self.loss_after_per_batch_random
                )
                if self.is_master:
                    tplr.log_with_context(
                        level="info",
                        message=f"Loss improvement (random data): {self.loss_improvement_random}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                self.relative_improvement_random = (
                    self.loss_improvement_random / self.loss_before_per_batch_random
                    if self.loss_before_per_batch_random > 0
                    else 0.0
                )
                if self.is_master:
                    tplr.log_with_context(
                        level="debug",
                        message=f"Relative improvement (random data): {self.relative_improvement_random}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                # Calculate original performance score (gradient quality)
                self.gradient_scores[eval_uid] = (
                    loss_before_random - loss_after_random
                ) / loss_before_random

                if self.is_master:
                    tplr.log_with_context(
                        level="debug",
                        message=f"Gradient Score: {self.gradient_scores[eval_uid]}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                    # Track negative evaluation history
                    self.track_negative_evaluation(eval_uid)

                # Initialize or update OpenSkill rating for this peer
                if eval_uid not in self.openskill_ratings and self.is_master:
                    self.openskill_ratings[eval_uid] = self.openskill_model.rating(
                        name=str(eval_uid)
                    )

                # Record the gradient score for later OpenSkill updates
                if not hasattr(self, "current_window_scores"):
                    self.current_window_scores = {}
                self.current_window_scores[eval_uid] = self.gradient_scores[
                    eval_uid
                ].item()

                # Calculate binary indicator for overfitting detection
                improvement_own = (
                    (loss_before_own - loss_after_own) / loss_before_own
                    if loss_before_own > 0
                    else 0
                )
                improvement_random = (
                    (loss_before_random - loss_after_random) / loss_before_random
                    if loss_before_random > 0
                    else 0
                )
                if self.is_master:
                    self.binary_indicator_scores[eval_uid] = (
                        1 if improvement_own > improvement_random else -1
                    )
                    tplr.log_with_context(
                        level="info",
                        message=f"Binary Indicator Score: {self.binary_indicator_scores[eval_uid]}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                    # Update binary moving average using exponential moving average formula:
                    # new_avg = (1-alpha) * old_avg + alpha * new_value
                    # where alpha is binary_score_ma_alpha hyperparameter
                    self.binary_moving_averages[eval_uid] = (
                        (1 - self.hparams.binary_score_ma_alpha)
                        * self.binary_moving_averages[eval_uid]
                        + self.hparams.binary_score_ma_alpha
                        * self.binary_indicator_scores[eval_uid]
                    )
                    tplr.log_with_context(
                        level="debug",
                        message=f"Binary Moving Average Score: {self.binary_moving_averages[eval_uid]}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                    sync_result = await self.evaluate_miner_sync(eval_uid)
                    sync_score = cast(
                        float,
                        sync_result.get("sync_score", 0.0),
                    )
                    self.log_sync_score(eval_uid, sync_result)

                    # Store the sync score for this miner
                    self.sync_scores[eval_uid] = sync_score

                self.evaluated_uids.add(eval_uid)

                evaluated_peers += 1
                uid_eval_time = time.time() - uid_eval_start
                if self.is_master:
                    tplr.log_with_context(
                        level="info",
                        message=f"{tplr.P(self.sync_window, tplr.T() - eval_start)} "
                        f"Completed evaluation | UID eval time: {uid_eval_time:.3f}s",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )

                # Synchronize all ranks at the end of each evaluation iteration
                dist_helper.safe_barrier("end_eval_iter", self.local_rank)

            del saved_state
            torch.cuda.empty_cache()
            self.sampler._cached_indices.clear()

            # Update eval_peers counters based on actual evaluation attempts
            # Reset counters for UIDs that were attempted (whether successful or not)
            if self.is_master:
                for uid in uids_attempted_this_window:
                    self.eval_peers[uid] = 1

                # Increment counters for peers that weren't attempted due to window change
                for uid in self.eval_peers.keys():
                    if uid not in uids_attempted_this_window:
                        self.eval_peers[uid] += 1

                self.comms.eval_peers = self.eval_peers

                # Log if some evaluations were skipped due to window exhaustion
                if len(uids_attempted_this_window) < len(evaluation_uids):
                    skipped_uids = list(
                        set(evaluation_uids) - uids_attempted_this_window
                    )
                    tplr.log_with_context(
                        level="info",
                        message=f"Window exhaustion: Skipped evaluation for UIDs {sorted(skipped_uids)}. Completed {len(uids_attempted_this_window)}/{len(evaluation_uids)} evaluations.",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                    )

            # elapsed time for full peer-evaluation loop
            evaluation_time = tplr.T() - eval_start

            # Barrier after evaluation completes
            dist_helper.safe_barrier("post_eval", self.local_rank)

            # Perform logging
            if self.is_master:
                _ = self.update_and_log_evals()

            # 17. Set weights periodically
            if self.sync_window % self.hparams.windows_per_weights == 0:
                # Only set weights for evaluated peers with non-negative (positive) weight values.
                positive_weighted_uids = sorted(
                    [uid for uid in self.evaluated_uids if self.weights[uid] > 0]
                )
                if (
                    self.hparams.burn_rate > 0
                    and self.weights[self.burn_uid] > 0
                    and self.burn_uid not in positive_weighted_uids
                ):
                    positive_weighted_uids.append(self.burn_uid)
                    positive_weighted_uids.sort()
                if positive_weighted_uids and self.is_master:
                    self.comms.subtensor.set_weights(
                        wallet=self.wallet,
                        netuid=cast(int, self.config.netuid),
                        uids=positive_weighted_uids,
                        weights=self.weights[positive_weighted_uids],
                        wait_for_inclusion=False,
                        wait_for_finalization=False,
                    )

            # Add barrier before model update to ensure all ranks are ready
            tplr.log_with_context(
                level="info",
                message="Pre-model-update barrier sync",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            dist_helper.safe_barrier("pre_model_update", self.local_rank)

            # 14. Now, merge the gathered gradients into the model AFTER finishing evaluation
            self.model.train()
            update_start = tplr.T()
            self.outer_optimizer.zero_grad()
            self.model.zero_grad()

            tplr.neurons.outer_step(
                self.model,
                self.outer_optimizer,
                gather_result=gather_result,
                transformer=self.transformer,
                compressor=self.compressor,
                xshapes=self.xshapes,
                totalks=self.totalks,
                device=cast(str, self.device),
                is_master=self.is_master,
                world_size=self.world_size,
                use_dct=self.hparams.use_dct,
                wandb_run=self.wandb if self.is_master else None,
                global_step=self.global_step,
            )

            # Add barrier after model update to ensure all ranks complete the update
            tplr.log_with_context(
                level="info",
                message="Post-model-update barrier sync",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            dist_helper.safe_barrier("post_model_update", self.local_rank)

            model_update_time = tplr.T() - update_start
            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message=f"{tplr.P(self.sync_window, model_update_time)} Updated model",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                # ------ NEW: gradient & weight-norm statistics (outer-step) ------------
                grad_norms, weight_norms = [], []
                for p in self.model.parameters():
                    if p.grad is not None:
                        grad_norms.append(p.grad.data.norm().item())
                        weight_norms.append(p.data.norm().item())

                if grad_norms:
                    mean_grad_norm = sum(grad_norms) / len(grad_norms)
                    max_grad_norm = max(grad_norms)
                    min_grad_norm = min(grad_norms)
                    median_grad_norm = float(np.median(grad_norms))
                    grad_norm_std = torch.tensor(grad_norms).std().item()
                else:  # remember: tensors → floats
                    mean_grad_norm = max_grad_norm = min_grad_norm = (
                        median_grad_norm
                    ) = grad_norm_std = 0.0

                mean_weight_norm = (
                    (sum(weight_norms) / len(weight_norms)) if weight_norms else 0.0
                )
                grad_to_weight_ratio = (
                    (mean_grad_norm / mean_weight_norm) if mean_weight_norm else 0.0
                )

                # Console / Loki
                tplr.log_with_context(
                    level="info",
                    message=(
                        f"GradNorm μ={mean_grad_norm:.4f} σ={grad_norm_std:.4f} "
                        f"median={median_grad_norm:.4f} min={min_grad_norm:.4f} "
                        f"max={max_grad_norm:.4f} | "
                        f"WeightNorm μ={mean_weight_norm:.4f} | "
                        f"g/w={grad_to_weight_ratio:.4f}"
                    ),
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            # ↳ WandB
            if self.is_master:
                self.wandb.log(
                    {
                        "gradient/mean_grad_norm": mean_grad_norm,
                        "gradient/max_grad_norm": max_grad_norm,
                        "gradient/min_grad_norm": min_grad_norm,
                        "gradient/median_grad_norm": median_grad_norm,
                        "gradient/grad_norm_std": grad_norm_std,
                        "gradient/mean_weight_norm": mean_weight_norm,
                        "gradient/grad_to_weight_ratio": grad_to_weight_ratio,
                    },
                    step=self.global_step,
                )

                # ↳ InfluxDB (metrics_logger)
                self.metrics_logger.log(
                    measurement="validator_gradient_stats",
                    tags={
                        "window": int(self.sync_window),
                        "global_step": int(self.global_step),
                    },
                    fields={
                        "mean_grad_norm": mean_grad_norm,
                        "max_grad_norm": max_grad_norm,
                        "min_grad_norm": min_grad_norm,
                        "median_grad_norm": median_grad_norm,
                        "grad_norm_std": grad_norm_std,
                        "mean_weight_norm": mean_weight_norm,
                        "grad_to_weight_ratio": grad_to_weight_ratio,
                    },
                    with_system_metrics=True,
                    with_gpu_metrics=True,
                )

            with torch.no_grad():
                slice_idx = slice(10, 12)  # indices published in miner debug dict

                for n, p in self.model.named_parameters():
                    if p.numel() < 12:
                        continue

                    # Handle DTensor case - get local tensor first
                    if isinstance(p, DT):
                        # For DTensor, use the local tensor
                        local_p = p.to_local()
                        curr_cpu = local_p.detach().cpu()
                    else:
                        # move current weights to CPU once
                        curr_cpu = p.detach().cpu()

                    # compute delta only if we have a previous slice
                    if n in self.prev_param_state:
                        prev_slice = self.prev_param_state[n]
                        curr_slice = curr_cpu.flatten()[slice_idx]

                        delta_slice = torch.abs(curr_slice - prev_slice)

                        # lazy-init & EMA update
                        if n not in self.param_avg_change:
                            self.param_avg_change[n] = delta_slice.clone()
                        else:
                            self.param_avg_change[n].mul_(
                                1 - self.param_change_alpha
                            ).add_(delta_slice * self.param_change_alpha)

                    # stash the new slice for next iteration
                    self.prev_param_state[n] = curr_cpu.flatten()[slice_idx].clone()

            # Add debug data including successfully gathered peers
            debug_dict = {}

            # Add model parameters debug info
            for name, param in self.model.named_parameters():
                if (
                    param is not None and param.numel() >= 2
                ):  # Check if tensor has at least 2 elements
                    # Handle DTensor case - get local tensor first
                    if isinstance(param, DT):
                        local_param = param.to_local()
                        debug_dict[name + "_debug"] = (
                            local_param.flatten()[:2].detach().cpu().tolist()
                        )
                    else:
                        debug_dict[name + "_debug"] = (
                            param.flatten()[:2].detach().cpu().tolist()
                        )

            # Add successful peers information
            if len(skipped_uids) > 0:
                debug_dict["successful_peers"] = sorted(
                    list(set(self.comms.peers) - set(skipped_uids))
                )
                debug_dict["skipped_peers"] = sorted(list(skipped_uids))

            # 15. Store debug values and model metadata
            if self.is_master:
                t = asyncio.create_task(
                    self.comms.put(
                        state_dict=debug_dict,
                        uid=str(self.uid),
                        window=self.sync_window,
                        key="debug",
                        local=False,
                    )
                )
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)

            window_total_time = tplr.T() - window_start
            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message=f"Stored debug values for window {self.current_window}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                if evaluated_peers == 0:
                    # Use the values from the previous step
                    avg_loss_before_per_batch_own = self.previous_avg_loss_before_own
                    avg_loss_after_per_batch_own = self.previous_avg_loss_after_own
                    avg_loss_before_per_batch_random = (
                        self.previous_avg_loss_before_random
                    )
                    avg_loss_after_per_batch_random = (
                        self.previous_avg_loss_after_random
                    )
                else:
                    # Calculate averages normally
                    avg_loss_before_per_batch_own /= evaluated_peers
                    avg_loss_after_per_batch_own /= evaluated_peers
                    avg_loss_before_per_batch_random /= evaluated_peers
                    avg_loss_after_per_batch_random /= evaluated_peers

                    # Store current values for future use when evaluated_peers might be 0
                    self.previous_avg_loss_before_own = avg_loss_before_per_batch_own
                    self.previous_avg_loss_after_own = avg_loss_after_per_batch_own
                    self.previous_avg_loss_before_random = (
                        avg_loss_before_per_batch_random
                    )
                    self.previous_avg_loss_after_random = (
                        avg_loss_after_per_batch_random
                    )

                # 16. Log evaluation metrics once all evaluations are done
                threshold_pct = int(round(self.hparams.idx_overlap_threshold * 100))
                evaluation_metrics = {
                    "validator/loss/own/before": avg_loss_before_per_batch_own,
                    "validator/loss/own/after": avg_loss_after_per_batch_own,
                    "validator/loss/random/before": avg_loss_before_per_batch_random,
                    "validator/loss/random/after": avg_loss_after_per_batch_random,
                    "validator/loss/own/improvement": self.relative_improvement_own,
                    "validator/loss/random/improvement": self.relative_improvement_random,
                    "validator/network/block": self.current_block,
                    "validator/network/window": self.sync_window,
                    "validator/network/step": self.global_step,
                    "validator/network/evaluated_uids": len(self.evaluated_uids),
                    "validator/optimizer/outer_lr": self.lr,
                    "validator/optimizer/inner_lr": current_inner_lr,
                    "validator/network/active_miners": len(self.valid_score_indices),
                    "validator/gather/success_rate": success_rate * 100,
                    "validator/timing/window_total": window_total_time,
                    "validator/timing/peer_update": peer_update_time,
                    "validator/timing/gather": gather_time,
                    "validator/timing/evaluation": evaluation_time,
                    "validator/timing/model_update": model_update_time,
                    "validator/overlap/pairs_checked": idx_overlap["pairs_checked"],
                    f"validator/overlap/pairs_over_{threshold_pct}": idx_overlap[
                        "pairs_high_ovlap"
                    ],
                    f"validator/overlap/ratio_over_{threshold_pct}": idx_overlap[
                        "ratio_high_ovlap"
                    ],
                    "validator/overlap/mean": idx_overlap["mean_overlap"],
                    "validator/overlap/min": idx_overlap["min_overlap"],
                    "validator/overlap/max": idx_overlap["max_overlap"],
                    # ── gather quality extras ────────────────────────────────
                    "validator/gather/intended_mean_final": mean_final_intended,
                    "validator/gather/actual_mean_final": mean_final_actual,
                    "validator/gather/reserve_used": reserve_used,
                }
                self.wandb.log(evaluation_metrics, step=self.global_step)

                # Log metrics to InfluxDB in parallel using primitive types
                gather_success_rate = float(success_rate * 100)
                total_skipped = len(skipped_uids)

                self.metrics_logger.log(
                    measurement="validator_window_v2",
                    tags={
                        "window": int(self.sync_window),
                        "global_step": int(self.global_step),
                    },
                    fields={
                        "loss_own_before": float(avg_loss_before_per_batch_own),
                        "loss_own_after": float(avg_loss_after_per_batch_own),
                        "loss_random_before": float(avg_loss_before_per_batch_random),
                        "loss_random_after": float(avg_loss_after_per_batch_random),
                        "loss_own_improvement": float(self.relative_improvement_own),
                        "loss_random_improvement": float(
                            self.relative_improvement_random
                        ),
                        "current_block": int(self.current_block),
                        "evaluated_uids_count": int(len(self.evaluated_uids)),
                        "outer_lr": float(self.lr),
                        "inner_lr": float(current_inner_lr),
                        "active_miners_count": int(len(self.valid_score_indices)),
                        "gather_success_rate": gather_success_rate,
                        "window_total_time": float(window_total_time),
                        "peer_update_time": float(peer_update_time),
                        "gather_time": float(gather_time),
                        "evaluation_time": float(evaluation_time),
                        "model_update_time": float(model_update_time),
                        "total_peers": int(len(self.comms.peers)),
                        "total_skipped": int(total_skipped),
                    },
                    with_system_metrics=True,
                    with_gpu_metrics=True,
                )
                tplr.log_with_context(
                    level="info",
                    message="Finished metrics logging call for validator",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
            # Log total window time and metrics
            tplr.log_with_context(
                level="info",
                message=f"{tplr.P(self.sync_window, window_total_time)} Completed window iteration",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )

            # Clean up memory before potential checkpoint creation
            if "gather_result" in locals() and gather_result is not None:
                del gather_result
            if "state_dict" in locals():
                del state_dict
            if "clip_norm_dict" in locals():
                del clip_norm_dict
            torch.cuda.empty_cache()

            # Synchronize all ranks before checkpoint
            dist_helper.safe_barrier("pre_checkpoint", self.local_rank)

            # 17. Create checkpoints periodically
            if (
                self.global_step % self.hparams.checkpoint_frequency == 0
                and self.global_step != 0
            ):
                tplr.log_with_context(
                    level="info",
                    message=f"Creating checkpoint at global_step {self.global_step}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

                handle = await self.ckpt.save_local_async(
                    model=self.model,
                    window=self.sync_window,
                    sync_window=self.sync_window,
                    topology="FSDP",
                )

                # Schedule an upload that will wait for the save to finish, then upload in background
                await self.ckpt.upload(
                    window=self.sync_window,
                    background=True,
                    delete_local_on_success=True,
                    wait_for=handle,
                )

            # Synchronize all ranks before moving to next window
            dist_helper.safe_barrier("pre_next_window", self.local_rank)

            # 19. Increment global step
            self.global_step += 1

            torch.cuda.empty_cache()

    def select_initial_peers(self) -> tuple[list[int], list[int]] | None:
        """
        Simple initial peer selection based on incentive.
        1) Select peers with highest incentive
        2) If needed, fill remaining slots with active peers
        3) Ensure we have minimum number of peers
        """
        try:
            tplr.log_with_context(
                level="info",
                message="Starting selection of initial gather peers",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )

            # 1. Create a dictionary of active peers with non-zero incentive
            uid_to_incentive = {}
            for uid, incentive in zip(
                self.comms.metagraph.uids.tolist(), self.comms.metagraph.I.tolist()
            ):
                if incentive > 0 and uid in self.comms.active_peers:
                    uid_to_incentive[uid] = float(incentive)

            reserve_cnt = self.hparams.reserve_peer_count
            total_needed = self.hparams.gather_peer_count + reserve_cnt

            # Sort by incentive (highest first) and take at most `total_needed`
            ranked = sorted(
                uid_to_incentive.keys(),
                key=lambda uid: uid_to_incentive[uid],
                reverse=True,
            )[:total_needed]

            gather_peers = ranked[: self.hparams.gather_peer_count]
            reserve_peers = ranked[self.hparams.gather_peer_count :]

            if len(gather_peers) + len(reserve_peers) == total_needed:
                tplr.log_with_context(
                    level="info",
                    message=(
                        f"Selected initial peers purely by incentive – "
                        f"gather:{gather_peers} reserve:{reserve_peers}"
                    ),
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                return gather_peers, reserve_peers

            # 2. If needed, fill up with active peers that don't have incentive
            remaining_active_peers = [
                int(peer) for peer in self.comms.active_peers if peer not in ranked
            ]

            # Calculate how many more peers we need
            needed_peers = total_needed - len(ranked)

            # Randomly select from remaining active peers
            additional_peers = random.sample(
                remaining_active_peers, min(len(remaining_active_peers), needed_peers)
            )

            # Combine the lists
            ranked.extend(additional_peers)

            # If still short, pad with more random actives (if any)
            if len(ranked) < total_needed:
                pad_needed = total_needed - len(ranked)
                pool = [p for p in self.comms.active_peers if p not in ranked]
                ranked.extend(random.sample(pool, min(pad_needed, len(pool))))

            gather_peers = ranked[: self.hparams.gather_peer_count]
            reserve_peers = ranked[self.hparams.gather_peer_count : total_needed]

            # Ensure we have enough peers
            if len(ranked) >= self.hparams.minimum_peers:
                tplr.log_with_context(
                    level="info",
                    message=(
                        f"Selected initial peers – "
                        f"gather:{gather_peers} reserve:{reserve_peers} "
                        f"(extra: {len(additional_peers)})"
                    ),
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                # No need to remove duplicates as we ensured lists don't overlap
                return gather_peers, reserve_peers

            # 3. If we don't have enough peers, give up
            tplr.log_with_context(
                level="info",
                message=f"Failed to select at least {self.hparams.minimum_peers} initial gather "
                f"peers. Found only {len(ranked)} active peers.",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            return None

        except Exception as e:
            tplr.log_with_context(
                level="error",
                message=f"Failed to create new peer list: {e}",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            return None

    def select_next_peers(self) -> tuple[list[int], list[int]] | None:
        """
        Simple peer selection that prioritizes the highest weights.
        1) Get all active peers
        2) Sort them by weight (highest first)
        3) Select up to gather_peer_count
        4) If not enough high-weight peers, fill remaining with random active peers
        """
        # Get all active peers as a list
        active_peers = [
            int(peer)
            for peer in self.comms.active_peers
            if peer not in self.naughty_peers
        ]

        # Check if we have enough active peers
        if len(active_peers) < self.hparams.minimum_peers:
            tplr.log_with_context(
                level="info",
                message=f"Not enough active peers ({len(active_peers)}) to meet minimum requirement ({self.hparams.minimum_peers})",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            return None

        # Create list of (peer_id, weight) tuples
        peer_weights = []
        for peer_id in active_peers:
            weight = float(self.weights.cpu()[peer_id])
            peer_weights.append((peer_id, weight))

        # Sort by weight (highest first)
        peer_weights.sort(key=lambda x: x[1], reverse=True)

        # Take peers with highest weights first
        reserve_cnt = self.hparams.reserve_peer_count
        total_needed = self.hparams.gather_peer_count + reserve_cnt

        picked = [uid for uid, _ in peer_weights[:total_needed]]
        gather_peers = picked[: self.hparams.gather_peer_count]
        reserve_peers = picked[self.hparams.gather_peer_count :]

        tplr.log_with_context(
            level="info",
            message=(
                f"Selected peers by weight – "
                f"gather:{gather_peers} reserve:{reserve_peers}"
            ),
            sync_window=self.sync_window,
            current_window=self.current_window,
        )

        return gather_peers, reserve_peers

    async def create_and_post_peers(self) -> None:
        initial_selection = False
        if (
            self.last_peer_update_window is None
            or self.sync_window - self.last_peer_update_window
            >= self.hparams.peer_replacement_frequency
        ):
            reason = (
                f"{self.last_peer_update_window=}"
                if self.last_peer_update_window is None
                else f"{self.sync_window=}>="
                f"{self.last_peer_update_window}+"
                f"{self.hparams.peer_replacement_frequency}="
                "self.last_peer_update_window+"
                "self.hparams.peer_replacement_frequency"
            )

            if self.is_master:
                tplr.log_with_context(
                    level="info",
                    message=f"Time to create and post a new peer list because {reason}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
            if self.last_peer_update_window is None:
                selected_peers = self.select_initial_peers()
                initial_selection = True
            else:
                selected_peers = self.select_next_peers()
            if selected_peers is not None and self.is_master:
                self.last_peer_update_window = self.sync_window
                gather_peers, reserve_peers = selected_peers
                await self.comms.post_peer_list(
                    peers=gather_peers,
                    reserve_peers=reserve_peers,
                    first_effective_window=self.current_window
                    + self.hparams.peer_list_window_margin,
                    sync_window=self.sync_window,
                    initial_selection=initial_selection,
                )
        return

    def penalize_inactive_peers(self) -> None:
        for uid, (inactive_since, _) in list(self.inactive_scores.items()):
            # If peer became active again, remove from inactive tracking
            if uid in self.eval_peers.keys():
                del self.inactive_scores[uid]
                tplr.log_with_context(
                    level="info",
                    message=f"UID {uid} became active again",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                continue

            if (
                self.current_window - inactive_since
                > self.hparams.reset_inactivity_windows
            ):
                self.reset_peer(uid)
                tplr.log_with_context(
                    level="info",
                    message=f"UID {uid} fully reset after extended inactivity",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                continue

            # Apply flat 25% penalty instead of exponential decay
            old_score = self.final_scores[uid].item()
            new_score = old_score  # Initialize new_score with old_score value
            if self.final_scores[uid] > 0:
                self.final_scores[uid] *= (
                    0.75  # Apply flat 25% reduction for positive scores only
                )

                new_score = self.final_scores[uid].item()

                tplr.log_with_context(
                    level="info",
                    message=f"UID {uid} penalized for inactivity: {old_score:.4f} -> {new_score:.4f}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            # Log slash metrics to WandB
            if self.is_master:
                self.wandb.log(
                    {
                        f"validator/inactivity/{uid}/score_before": old_score,
                        f"validator/inactivity/{uid}/score_after": new_score,
                    },
                    step=self.global_step,
                )

                # Log slash metrics to InfluxDB with primitive types
                self.metrics_logger.log(
                    measurement="validator_inactivity",
                    tags={
                        "uid": str(uid),
                        "window": int(self.current_window),
                        "global_step": int(self.global_step),
                    },
                    fields={
                        "score_before": float(old_score),
                        "score_after": float(new_score),
                    },
                    with_system_metrics=True,
                    with_gpu_metrics=True,
                )
        return

    async def evaluate_miner_sync(
        self, eval_uid: int
    ) -> dict[str, bool | float | int | str]:
        """
        Evaluates the synchronization of a specific miner with the validator's model.

        Args:
            validator: The validator instance
            eval_uid: The UID of the miner to evaluate

        Returns:
            dict: Synchronization metrics and score
        """
        # Fetch the miner's debug dictionary
        debug_result = await self.comms.get(
            uid=str(eval_uid),
            window=self.sync_window - 1,
            key="debug",
            local=False,
            stale_retention=10,
        )

        # Check if we got a valid result
        if not debug_result.success:
            return {
                "success": False,
                "error": "Failed to retrieve debug dictionary",
                "sync_score": 0.0,
            }

        miner_debug_dict = cast(dict, debug_result.data)

        # Validate debug dictionary format
        if miner_debug_dict is None or not isinstance(miner_debug_dict, dict):
            return {
                "success": False,
                "error": "Invalid debug dictionary format",
                "sync_score": 0.0,
            }

        # Compare miner's debug dict with validator's model
        comparison_metrics = await tplr.neurons.compare_model_with_debug_dict(
            model=self.model,
            debug_dict=miner_debug_dict,
            learning_rate=self.lr,
            index_range=(10, 12),
            param_avg_change=self.param_avg_change,
        )

        if not comparison_metrics["success"]:
            return {
                "success": False,
                "error": "Failed to compare model with debug dictionary",
                "sync_score": 0.0,
            }

        # Calculate sync score using the formula: score = (1-x/5)^2.5
        # where x is the average steps behind, capped at 5
        avg_steps_behind = comparison_metrics["avg_steps_behind"]
        x = min(avg_steps_behind, 5.0)
        sync_score = max(0.0, (1.0 - x / 5.0) ** 2.5)

        # Add the sync score to the metrics
        result = {**comparison_metrics, "sync_score": sync_score}

        return result

    def update_and_log_evals(self) -> None:
        self.update_openskill_ratings()
        self.update_weights()
        # Log scores and metrics for evaluated UIDs as a table
        headers = [
            "UID",
            "Last eval window",
            "Gradient Score",
            "Binary Indicator",
            "Binary Moving Avg",
            "Final Score",
            "Sync score",
            "Weight",
            "OpenSkill",
        ]
        table = [headers]
        for uid in sorted(self.evaluated_uids):
            openscore_info = "N/A"
            if uid in self.openskill_ratings:
                rating = self.openskill_ratings[uid]
                openscore_info = (
                    f"{rating.ordinal():.2f} (μ={rating.mu:.1f}, σ={rating.sigma:.1f})"
                )
            row = [
                str(uid),
                f"{self.peers_last_eval_window[uid]}",
                f"{self.gradient_scores[uid]:.6f}",
                f"{self.binary_indicator_scores[uid]:.4f}",
                f"{self.binary_moving_averages[uid]:.4f}",
                f"{self.final_scores[uid]:.4f}",
                f"{self.sync_scores[uid]:.4f}",
                f"{self.weights[uid]:.4f}",
                openscore_info,
            ]
            table.append(row)

        try:
            try:
                width = os.get_terminal_size().columns
            except Exception:
                width = 0
            os.environ["COLUMNS"] = str(max(200, width))

            rich_table = Table(title="Updated scores for evaluated UIDs")
            for header in headers:
                rich_table.add_column(header)
            for row in table[1:]:
                rich_table.add_row(*row)
            sio = StringIO()
            console = Console(file=sio, width=int(os.environ["COLUMNS"]))
            console.print(rich_table)
            table_str = sio.getvalue()
        except ImportError:
            tplr.log_with_context(
                level="warning",
                message="rich module not found; falling back to basic formatting.",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            col_widths = [
                max(len(row[i]) for row in table) for i in range(len(headers))
            ]
            lines = []
            for i, row in enumerate(table):
                line = " | ".join(row[j].ljust(col_widths[j]) for j in range(len(row)))
                lines.append(line)
                if i == 0:
                    separator = "-+-".join(
                        "-" * col_widths[j] for j in range(len(headers))
                    )
                    lines.append(separator)
            table_str = "\n".join(lines)

        tplr.log_with_context(
            level="info",
            message="Updated scores for evaluated UIDs:\n" + table_str,
            sync_window=self.sync_window,
            current_window=self.current_window,
        )

        should_downsample = len(self.evaluated_uids) > 8
        for uid in sorted(self.evaluated_uids):
            # Extract primitive values from tensors for WandB
            gradient_score = float(self.gradient_scores[uid].item())
            binary_indicator = float(self.binary_indicator_scores[uid].item())
            binary_moving_avg = float(self.binary_moving_averages[uid].item())
            sync_score = float(self.sync_scores[uid].item())
            final_score = float(self.final_scores[uid].item())
            weight = float(self.weights[uid].item())

            if self.is_master:
                self.wandb.log(
                    {
                        f"validator/gradient_scores/{uid}": gradient_score,
                        f"validator/binary_indicators/{uid}": binary_indicator,
                        f"validator/binary_moving_averages/{uid}": binary_moving_avg,
                        f"validator/final_scores/{uid}": final_score,
                        f"validator/sync_score/{uid}": sync_score,
                        f"validator/weights/{uid}": weight,
                    },
                    step=self.global_step,
                )

                self.metrics_logger.log(
                    measurement="validator_scores",
                    tags={
                        "eval_uid": str(uid),
                        "window": int(self.sync_window),
                        "global_step": int(self.global_step),
                    },
                    fields={
                        "gradient_score": gradient_score,
                        "binary_indicator": binary_indicator,
                        "binary_moving_avg": binary_moving_avg,
                        "sync_score": sync_score,
                        "final_score": final_score,
                        "weight": weight,
                    },
                    with_system_metrics=True,
                    with_gpu_metrics=True,
                    sample_rate=0.8 if should_downsample else 1.0,
                )
        return

    def slash_for_missing_gradient(self, eval_uid: int) -> None:
        """Slash a peer for not submitting a gradient.

        Args:
            eval_uid: The UID of the peer to slash
        """
        if not self.is_master:
            return

        tplr.log_with_context(
            level="info",
            message=f"No gradient received from UID {eval_uid}. Slashing moving average score by {1 - self.missing_gradient_slash_rate:.2%}",
            sync_window=self.sync_window,
            current_window=self.current_window,
            eval_uid=eval_uid,
        )
        old_score = self.final_scores[eval_uid].item()

        if self.final_scores[eval_uid] > 0:
            self.final_scores[eval_uid] *= self.missing_gradient_slash_rate
            self.binary_moving_averages[eval_uid] *= self.missing_gradient_slash_rate

            new_score = self.final_scores[eval_uid].item()
            tplr.log_with_context(
                level="info",
                message=f"Reduced score of UID {eval_uid} from {old_score:.4f} to {new_score:.4f} due to missing gradient.",
                sync_window=self.sync_window,
                current_window=self.current_window,
                eval_uid=eval_uid,
            )
        else:
            tplr.log_with_context(
                level="info",
                message=f"Skipped reducing score of UID {eval_uid} (current score: {old_score:.4f}) due to negative or zero value.",
                sync_window=self.sync_window,
                current_window=self.current_window,
                eval_uid=eval_uid,
            )

        # Ensure the UID is included in evaluated_uids only when penalized
        self.evaluated_uids.add(eval_uid)

        # Log updated scores
        tplr.log_with_context(
            level="info",
            message="Updated scores for evaluated UIDs after slashing:",
            sync_window=self.sync_window,
            current_window=self.current_window,
        )
        # Log evaluated UID scores (fixed join call)
        line = " | ".join(
            f"UID {uid}: {self.final_scores[uid]:.4f}"
            for uid in sorted(self.evaluated_uids)
        )
        tplr.logger.info(line)

        # Log to WandB
        self.wandb.log(
            {
                f"validator/final_scores/{eval_uid}": self.final_scores[
                    eval_uid
                ].item(),
                f"validator/weights/{eval_uid}": self.weights[eval_uid].item(),
            },
            step=self.global_step,
        )

    def slash_for_invalid_gradient(self, eval_uid: int, error: Exception) -> None:
        """Slash a peer for providing invalid gradient data.

        Args:
            eval_uid: The UID of the peer to slash
            error: The exception that was raised during gradient validation
        """
        if not self.is_master:
            return

        old_score = self.final_scores[eval_uid].item()

        if old_score > 0:
            # Reset positive scores to zero explicitly
            self.final_scores[eval_uid] = 0.0
            tplr.log_with_context(
                level="warning",
                message=f"Set positive score of UID {eval_uid} from {old_score:.4f} to 0.0 - invalid gradient data",
                sync_window=self.sync_window,
                current_window=self.current_window,
                eval_uid=eval_uid,
            )
        else:
            # Negative score is worse than zero; keep it as-is.
            tplr.log_with_context(
                level="warning",
                message=f"UID {eval_uid} had negative score {old_score:.4f}; retaining due to invalid gradient data",
                sync_window=self.sync_window,
                current_window=self.current_window,
                eval_uid=eval_uid,
            )

        # Include in evaluated UIDs so it gets logged in metrics
        self.evaluated_uids.add(eval_uid)

        # Log to WandB
        self.wandb.log(
            {
                f"validator/slash/{eval_uid}/score_before": old_score,
                f"validator/slash/{eval_uid}/score_after": self.final_scores[
                    eval_uid
                ].item(),
                f"validator/slash/{eval_uid}/reason": str(error),
            },
            step=self.global_step,
        )

        # Log to InfluxDB metrics with primitive types
        self.metrics_logger.log(
            measurement="validator_slash",
            tags={
                "eval_uid": str(eval_uid),
                "window": int(self.sync_window),
                "global_step": int(self.global_step),
                "reason_code": "invalid_gradient",
            },
            fields={
                "score_before": float(old_score),
                "score_after": float(self.final_scores[eval_uid].item()),
                "reason": str(error)[:255],  # Truncate long error messages
            },
            with_system_metrics=True,
            with_gpu_metrics=True,
        )

    def validate_gradient_data(
        self,
        model: torch.nn.Module,
        eval_uid: int,
        eval_state_dict: dict,
    ) -> None:
        """Validate all gradient data before applying any updates.

        Args:
            model: The model whose parameters are being validated
            eval_uid: The UID of the peer being evaluated
            eval_state_dict: The state dict containing gradient data

        Raises:
            ValueError: If any gradient data is invalid
        """
        for n, p in model.named_parameters():
            idxs_key = n + "idxs"
            vals_key = n + "vals"
            quant_key = n + "quant_params"
            idxs = eval_state_dict.get(idxs_key, None)
            vals = eval_state_dict.get(vals_key, None)
            quant_params = eval_state_dict.get(quant_key, None)

            if idxs is not None and vals is not None and quant_params is not None:
                # Handle 12-bit packed format: (packed_tensor, original_shape)
                if isinstance(idxs, tuple) and len(idxs) == 2:
                    packed_data, original_shape = idxs
                    # Move packed data to device
                    packed_data = packed_data.to(self.device)
                    idxs = (packed_data, original_shape)
                vals = vals.to(self.device)

                # Validate indices are within bounds
                if self.totalks.get(n) is None:
                    tplr.log_with_context(
                        level="warning",
                        message=f"Missing totalk for parameter {n}, skipping peer {eval_uid}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )
                    raise ValueError(
                        f"Invalid gradient data from peer {eval_uid}: Missing totalk for parameter {n}"
                    )

                # Check compressed indices are valid
                self.comms.check_compressed_indices(
                    idxs_key,
                    idxs,
                    self.totalks[n],
                    allowed_topk=self.hparams.topk_compression,
                    vals=vals,
                )

                # Check for NaN or Inf values
                if torch.isnan(vals).any() or torch.isinf(vals).any():
                    tplr.log_with_context(
                        level="warning",
                        message=f"Values contain NaN or Inf for parameter {vals_key}, skipping peer {eval_uid}",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )
                    raise ValueError(
                        f"Invalid gradient data from peer {eval_uid}: NaN or Inf values in {vals_key}"
                    )

    def update_model_with_gradient(
        self,
        model: torch.nn.Module,
        eval_uid: int,
        eval_state_dict: dict,
        clip_norm_dict: dict[str, torch.Tensor],
    ) -> None:
        model.zero_grad()

        clip_norm = True  # Always true in the repo 8/13/2025
        # If all validations pass, apply the gradients

        for n, p in model.named_parameters():
            src_rank = 0
            on_src = self.is_master or not dist_helper.is_distributed()

            full_grad_src = torch.empty(1, dtype=p.dtype, device=p.device)
            has_valid_gradient = True

            # Build the full dense grad on the source rank only (or always in single GPU)
            if on_src:
                idxs_key = n + "idxs"
                vals_key = n + "vals"
                quant_key = n + "quant_params"
                idxs = eval_state_dict.get(idxs_key, None)
                vals = eval_state_dict.get(vals_key, None)
                quant_params = eval_state_dict.get(quant_key, None)

                if clip_norm:
                    if vals is None or quant_params is None:
                        has_valid_gradient = False

                    if has_valid_gradient:
                        # convert to lists since not a list of matrices for dequant
                        vals = [vals]
                        quant_params = [quant_params]

                        vals_f32 = self.compressor.maybe_dequantize_values(
                            vals, quant_params, p.device
                        )
                        vals_f32 = vals_f32[0]  # Get the first (and only) tensor

                        eval_norm = torch.norm(vals_f32, p=2).to(p.device)

                        clip_norm_val = clip_norm_dict.get(vals_key, eval_norm)

                        clip_factor = torch.clamp(
                            clip_norm_val / (eval_norm + 1e-8), max=1
                        )

                        vals = vals_f32 * clip_factor

                if (
                    has_valid_gradient
                    and idxs is not None
                    and vals is not None
                    and quant_params is not None
                ):
                    if clip_norm:
                        quant_params = None  # Fast route for decompress

                    # Check if we're in distributed mode
                    # Use empty_like to avoid copying the param; just provide dtype/device/shape
                    ref = torch.empty_like(p, device=self.device, dtype=p.dtype)

                    decompressed = self.compressor.decompress(
                        ref,
                        idxs,
                        vals,
                        self.xshapes[n],
                        self.totalks[n],
                        quant_params,
                    )

                    full_grad_src = self.transformer.decode(
                        decompressed, use_dct=self.hparams.use_dct
                    )
                    # Single conversion to target dtype+device to avoid extra temporaries
                    full_grad_src = full_grad_src.to(
                        dtype=p.dtype, device=p.device, non_blocking=True
                    )

                    # Free intermediate pieces ASAP
                    del ref, decompressed

                    # Final safety check on the gradient itself
                    if (
                        torch.isnan(full_grad_src).any()
                        or torch.isinf(full_grad_src).any()
                    ):
                        tplr.log_with_context(
                            level="warning",
                            message=f"Decompressed gradient for {n} contains NaN/Inf, skipping peer {eval_uid}",
                            sync_window=self.sync_window,
                            current_window=self.current_window,
                            eval_uid=eval_uid,
                        )
                        del full_grad_src
                        has_valid_gradient = False

            # Broadcast gradient validity to all ranks immediately
            valid_tensor = torch.tensor(
                [has_valid_gradient], dtype=torch.bool, device=self.device
            )
            dist_helper.broadcast(valid_tensor, src=0)
            has_valid_gradient = bool(valid_tensor.item())

            # If gradient is invalid, all ranks raise exception together
            if not has_valid_gradient:
                raise ValueError(
                    f"Invalid gradient from peer {eval_uid}: Missing or invalid gradient data for {n}"
                )

            # Distribute gradient for DTensor or apply directly for regular tensors
            if isinstance(p, DT):
                # Ensure full_grad_src has correct dtype on source rank
                if on_src and full_grad_src.dtype != p.dtype:
                    full_grad_src = full_grad_src.to(dtype=p.dtype)

                src_tensor = (
                    full_grad_src
                    if on_src
                    else torch.empty(p.shape, device=p.device, dtype=p.dtype)
                )

                new_grad = distribute_tensor(
                    src_tensor,
                    device_mesh=p.device_mesh,
                    placements=p.placements,
                    src_data_rank=src_rank,
                )
                # master no longer needs the full dense grad
                if on_src:
                    del full_grad_src
                    full_grad_src = None

                # quick sanity (view, no extra big alloc)
                local_view = new_grad.to_local()
                if not torch.isfinite(local_view).all():
                    del new_grad, local_view
                    torch.cuda.empty_cache()
                    # Don't continue here - let the gradient be zero or handle it properly
                    # This prevents rank-specific skipping which causes deadlocks
                    tplr.log_with_context(
                        level="warning",
                        message=f"Non-finite gradient detected for {n}, setting to zero",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                        eval_uid=eval_uid,
                    )
                    # Skip this parameter update but don't break the loop
                else:
                    # Apply update directly to data
                    p.data.sub_(
                        new_grad,
                        alpha=self.lr * self.hparams.eval_lr_factor,
                    )
                del new_grad, local_view
            else:
                # Single GPU case (non-DTensor)
                if on_src:
                    p.data.sub_(
                        full_grad_src,
                        alpha=self.lr * self.hparams.eval_lr_factor,
                    )
                    del full_grad_src

    def compute_peer_val_norms(
        self,
        gather_result: SimpleNamespace,
    ) -> dict[str, torch.Tensor]:
        # clip_norm is basically always true in the repo 8/13/2025
        # In this case, leave it to refactor later
        clip_norm = True

        clip_norm_dict = {}
        state_dict = gather_result.state_dict
        if not state_dict:
            raise ValueError("Must have gather_result.state_dict to compute norms")

        for n, p in self.model.named_parameters():
            vals_key = n + "vals"
            quant_key = n + "quant_params"

            vals = getattr(state_dict, vals_key, None)
            quant_params = getattr(state_dict, quant_key, None)

            if vals is None:
                continue

            vals_f32 = self.compressor.maybe_dequantize_values(
                vals, quant_params, p.device
            )

            norms = torch.stack([torch.norm(v, p=2) for v in vals_f32]).to(p.device)
            clip_norm_dict[vals_key] = torch.median(norms)

        return clip_norm_dict

    # ------------- state helpers ----------------
    def _state_dict(self) -> dict:
        """Return cpu tensors ready for torch.save."""
        return {
            "global_step": self.global_step,
            "gradient_scores": self.gradient_scores.cpu(),
            "sync_scores": self.sync_scores.cpu(),
            "binary_indicator_scores": self.binary_indicator_scores.cpu(),
            "final_scores": self.final_scores.cpu(),
            "binary_moving_averages": self.binary_moving_averages.cpu(),
            "weights": self.weights.cpu(),
            # Store OpenSkill statistics per‑uid so we can fully restore them.
            # ordinal is redundant (mu/σ ⇒ ordinal) but handy for debugging.
            "openskill_ratings": {
                int(uid): {
                    "mu": float(r.mu),
                    "sigma": float(r.sigma),
                    "ordinal": float(r.ordinal()),
                }
                for uid, r in self.openskill_ratings.items()
            },
        }

    async def save_state(self):
        """Saves the current validator state to disk asynchronously."""
        try:
            tplr.log_with_context(
                level="info",
                message="Saving validator state",
            )
            # Run the blocking torch.save in a separate thread
            await asyncio.to_thread(torch.save, self._state_dict(), self.state_path)
        except Exception as e:
            tplr.log_with_context(
                level="warning",
                message=f"Failed to save validator state: {e}",
            )

    def load_state(self):
        """Loads the validator state from disk.

        This method deserializes the validator's state from the configured state path
        and updates the validator's internal state variables. The state includes:
        - global_step: Training iteration counter
        - gradient_scores: Scores based on gradient quality
        - sync_scores: Scores based on synchronization performance
        - binary_indicator_scores: Binary classification scores
        - final_scores: Combined final evaluation scores
        - binary_moving_averages: Moving averages of binary indicators
        - weights: Peer weighting values
        - openskill_ratings: Dictionary mapping UIDs to OpenSkill rating objects
          that track each peer's skill level using a Bayesian rating system.
          Each rating contains:
          - mu: Mean skill estimate (higher is better)
          - sigma: Uncertainty in the skill estimate (lower means more certainty)
          - ordinal: Conservative skill estimate (mu - n*sigma) used for ranking

        All tensors are converted to float and moved to the configured device.
        Exceptions during loading are caught and logged as warnings.
        """
        tplr.logger.info("Loading validator state")

        # ── stage 1: read file ─────────────────────────────────────────────
        try:
            state = torch.load(self.state_path, map_location=self.device)
        except FileNotFoundError:
            tplr.logger.warning(f"No validator state found at {self.state_path}")
            return
        except Exception as e:
            tplr.logger.warning(f"Failed to deserialize validator state: {e}")
            return

        # ── stage 2: selectively hydrate fields ────────────────────────────
        # NOTE: use `.get` so missing keys don't blow up wrong‑schema tests.
        self.global_step = int(
            state.get("global_step", getattr(self, "global_step", 0))
        )

        def _maybe(name):
            if name in state:
                setattr(
                    self,
                    name,
                    state[name].float().to(self.device),
                )

        for _tensor in (
            "gradient_scores",
            "sync_scores",
            "binary_indicator_scores",
            "final_scores",
            "binary_moving_averages",
            "weights",
        ):
            _maybe(_tensor)

        # ── OpenSkill ratings ──────────────────────────────────────────────
        try:
            saved_os = state.get("openskill_ratings", {})
            self.openskill_ratings = {
                int(uid): self.openskill_model.rating(
                    mu=float(osd["mu"]), sigma=float(osd["sigma"]), name=str(uid)
                )
                for uid, osd in saved_os.items()
            }
            tplr.logger.info(
                f"Restored OpenSkill ratings for {len(self.openskill_ratings)} peers"
            )
        except Exception as e:
            tplr.logger.warning(f"Failed to restore OpenSkill ratings: {e}")

    async def load_checkpoint(self) -> None:
        checkpoint_window_buffer = 5
        has_new_checkpoint = (
            self.global_step
            >= self.hparams.checkpoint_frequency + checkpoint_window_buffer
        )
        # Proceed to load checkpoint
        #   • rank-0 (or single-GPU run) downloads & catches-up
        #   • remaining ranks receive state via NCCL broadcast
        ckpt_sync_win = await self.ckpt.download_and_load(
            model=self.model,
            window=None,  # latest
            shared_fs=True,
            process_group=None,
            prefer_highest_staked=True,
        )
        ckpt_ok = ckpt_sync_win is not None

        assert self.start_window is not None
        if ckpt_ok:
            tplr.logger.info(f"Checkpoint loaded (sync_window={ckpt_sync_win})")
        else:
            tplr.logger.info("No checkpoint found – starting from scratch")

        # Decide catch-up windows and run catch-up on ALL ranks (avoids DTensor collective hangs)
        need_catchup = (not ckpt_ok) or (
            ckpt_ok
            and ckpt_sync_win < self.current_window
            and self.global_step > checkpoint_window_buffer
        )

        if need_catchup:
            start_from = (
                self.start_window
                if not ckpt_ok
                else max(ckpt_sync_win, self.start_window)
            )
            tplr.logger.info(
                f"Checkpoint is behind current window ({ckpt_sync_win} < {self.current_window}), starting catchup..."
            )
            await tplr.neurons.catchup_with_aggregation_server(self, start_from)
        else:
            tplr.logger.info("Checkpoint is up-to-date, skipping catchup.")

        # Replay scheduler steps if checkpoint was loaded
        if ckpt_ok:
            steps_to_replay = (
                ckpt_sync_win - self.start_window + 1
            ) * self.hparams.inner_steps
            for _ in range(steps_to_replay):
                self.inner_scheduler.step()

        return

    def bin_evaluation_peers(self, num_bins: int) -> dict[int, list[int]]:
        """
        Bins evaluation peers based on their performance metrics.
        Peers are grouped into bins of similar performance.

        Args:
            num_bins (int): Number of bins to divide peers into

        Returns:
            dict: Dictionary mapping bin indices to lists of peer UIDs
        """
        # Get all active peers
        active_peers = list(self.eval_peers.keys())

        # If we don't have enough peers, return a single bin with all peers
        if len(active_peers) < num_bins * self.hparams.uids_per_window:
            tplr.log_with_context(
                level="info",
                message=f"Not enough active peers ({len(active_peers)}) for {num_bins} bins. Using single bin.",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            return {0: active_peers}

        # Collect performance metrics for binning
        peer_metrics = []
        for uid in active_peers:
            metric = (
                float(self.openskill_ratings[uid].ordinal())
                if uid in self.openskill_ratings
                else 0.0
            )
            peer_metrics.append((uid, metric))

        # Sort peers by metric (highest first)
        peer_metrics.sort(key=lambda x: x[1], reverse=True)
        sorted_peers = [uid for uid, _ in peer_metrics]

        total_peers = len(sorted_peers)
        peers_per_bin = total_peers // num_bins
        remainder = total_peers % num_bins

        # Create bins with all peers distributed
        bins = {}
        start_idx = 0

        for i in range(num_bins):
            # Add one extra peer to the first 'remainder' bins
            bin_size = peers_per_bin + (1 if i < remainder else 0)
            end_idx = start_idx + bin_size

            # Skip empty bins
            if start_idx >= len(sorted_peers):
                continue

            # Ensure we don't go beyond the array bounds
            end_idx = min(end_idx, len(sorted_peers))

            bins[i] = sorted_peers[start_idx:end_idx]
            start_idx = end_idx

        # Verify all peers are assigned
        total_assigned = sum(len(peers) for peers in bins.values())
        if total_assigned != total_peers:
            tplr.log_with_context(
                level="warning",
                message=f"Bin assignment error: {total_assigned} peers assigned out of {total_peers}",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )

        # Log the bins for debugging
        for bin_idx, peer_list in bins.items():
            tplr.log_with_context(
                level="info",
                message=f"Bin {bin_idx} (size {len(peer_list)}): {peer_list}",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )

        return bins

    def select_next_bin_for_evaluation(self, num_bins: int) -> int:
        """
        Randomly select a bin to evaluate in the current window.

        Args:
            num_bins (int): Total number of bins

        Returns:
            int: The bin index to evaluate in this window
        """
        next_bin = random.randint(0, num_bins - 1)

        tplr.log_with_context(
            level="info",
            message=f"Randomly selected bin {next_bin} for evaluation in window {self.sync_window}",
            sync_window=self.sync_window,
            current_window=self.current_window,
        )

        return next_bin

    def select_evaluation_uids_from_bin(
        self, bins: dict[int, list[int]], bin_idx: int
    ) -> list[int]:
        """
        Selects peers for evaluation from a specific bin using weighted sampling.

        Args:
            bins (dict): Dictionary mapping bin indices to lists of peer UIDs
            bin_idx (int): The specific bin to select peers from

        Returns:
            list: Selected UIDs for evaluation
        """
        # Ensure the bin exists
        if bin_idx not in bins:
            tplr.log_with_context(
                level="warning",
                message=f"Bin {bin_idx} not found. Using bin 0 instead.",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            bin_idx = 0

        # Get peers in the selected bin
        bin_peers = bins[bin_idx]

        # Get weights for weighted sampling
        candidate_uids = list(bin_peers)
        candidate_weights = [self.eval_peers[uid] for uid in candidate_uids]

        # Determine how many peers to select (either all in the bin or uids_per_window)
        k = min(self.hparams.uids_per_window, len(candidate_uids))

        # Use weighted random sampling
        selected_uids = self.comms.weighted_random_sample_no_replacement(
            candidate_uids, candidate_weights, k
        )

        tplr.log_with_context(
            level="info",
            message=f"Selected {len(selected_uids)} evaluation UIDs from bin {bin_idx}",
            sync_window=self.sync_window,
            current_window=self.current_window,
        )

        return selected_uids

    def _training_pool_digest(self, uid: int, window: int) -> tuple[str, int]:
        """
        Re-create the *exact* index pool a miner used for (uid, window) and
        return a 128-bit hex digest **plus the expected sample count**.
        """
        miner_sampler = tplr.MinerSampler(
            dataset=self.dataset,
            uid=uid,
            window=window,
            steps_per_window=self.hparams.inner_steps,
            micro_bs=self.hparams.micro_batch_size,
            batch_size=self.hparams.batch_size,
            target_batch_size=self.hparams.target_batch_size,
            rank=0,
            world_size=1,
        )
        idxs = miner_sampler._global_indices()
        ids = miner_sampler.ids_for_indices(idxs.tolist())
        h = hashlib.blake2b(digest_size=16)
        h.update(np.asarray(sorted(ids), dtype=np.uint64).tobytes())
        return h.hexdigest(), len(ids)

    # ────────────────────────────────────────────────────────────────────
    # new helper: quick check & log
    # ────────────────────────────────────────────────────────────────────
    def log_digest_match(self, uid: int, meta: dict[str, object]) -> bool:
        """
        Compare miner-supplied metadata with our own expectation.
        Returns True on match, False on mismatch (and logs both cases).
        """
        mine, n_expected = self._training_pool_digest(uid, self.sync_window)

        his = meta.get("sample_digest")
        n_his = meta.get("sample_count")

        ok = (his == mine) and (n_his == n_expected)
        msg = (
            f"✅ sample_digest MATCH for UID {uid} (count {n_his}/{n_expected})"
            if ok
            else f"❌ sample_digest MISMATCH for UID {uid}\n"
            f"     expected {mine} ({n_expected})\n"
            f"     got      {his} ({n_his})"
        )
        level = "info" if ok else "warning"
        tplr.log_with_context(
            level=level,
            message=msg,
            sync_window=self.sync_window,
            current_window=self.current_window,
            eval_uid=uid,
        )
        return ok

    async def upload_gather_results(self, gather_result: SimpleNamespace) -> None:
        def to_cpu(obj):
            """Recursively move all tensors in an arbitrary container to CPU."""
            if torch.is_tensor(obj):
                return obj.detach().cpu()
            if isinstance(obj, (list, tuple)):
                return type(obj)(to_cpu(x) for x in obj)
            if isinstance(obj, dict):
                return {k: to_cpu(v) for k, v in obj.items()}
            return obj  # leave ints, floats, strings … untouched

        if self.is_master and self.uid == self.comms.metagraph.S.argmax().item():
            try:
                raw_state = gather_result.state_dict
                # Accept both SimpleNamespace and plain dict
                if isinstance(raw_state, SimpleNamespace):
                    raw_state = vars(raw_state)  # same as .__dict__

                cpu_state = {k: to_cpu(v) for k, v in raw_state.items()}
                payload = {
                    # SimpleNamespace → plain dict for Torch serialization
                    "state_dict": cpu_state,
                    "uids": gather_result.uids,
                    "skipped_uids": gather_result.skipped_uids,
                    "success_rate": gather_result.success_rate,
                }

                await self.comms.put(
                    state_dict=payload,
                    window=self.sync_window,
                    key="aggregator",
                    local=False,
                )

                tplr.log_with_context(
                    level="info",
                    message=f"Uploaded aggregated gradients for window {self.sync_window}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
            except Exception as e:
                tplr.log_with_context(
                    level="warning",
                    message=f"Failed to upload aggregated gradients: {e}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

    def check_deregistered_uids(self, idx_overlap_peers: dict) -> dict:
        """
        Find updates in metagraph that indicate a uid is a net-new user
        to avoid unintentionally punishing new miners where the old
        miner was naughty

        Args:
            idx_overlap_peers: The combined uids of the previously
                naughty peers and the current peers that exceed
                overlap threshold

        Returns:
            Updated idx_overlap_peers, keeping in mind deregistering
        """
        found_uids = list(idx_overlap_peers.keys())
        latest_hotkeys = dict(
            zip(self.comms.metagraph.uids, self.comms.metagraph.hotkeys)
        )

        for uid in found_uids:
            if (
                latest_hotkeys.get(uid) != self.current_hotkeys.get(uid)
                and idx_overlap_peers.get(uid)
                != "mega"  # not actively cheating already
            ):
                # peer has changed from deregistering
                self.naughty_peers.pop(uid, None)
                idx_overlap_peers.pop(uid, None)

        self.current_hotkeys = latest_hotkeys
        return idx_overlap_peers

    async def slash_for_poor_sync(self) -> None:
        """
        Slash peers that are too far behind in synchronization.
        Evaluates miner sync and applies penalties for being out of sync.
        """
        gather_sync_scores = await asyncio.gather(
            *(self.evaluate_miner_sync(uid) for uid in self.comms.peers)
        )

        for score_info, uid in zip(gather_sync_scores, self.comms.peers):
            avg_steps_behind = score_info.get("avg_steps_behind", 99.0)
            success = score_info.get("success", False)
            if not success or avg_steps_behind > self.hparams.sync_max_steps_behind:
                tplr.log_with_context(
                    level="info",
                    message=f"Slashing {uid}: avg_steps_behind={avg_steps_behind:.2f} > max={self.hparams.sync_max_steps_behind}",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )
                if self.final_scores[uid] > 0:
                    self.final_scores[uid] *= self.sync_score_slash_rate
                    self.binary_moving_averages[uid] *= self.sync_score_slash_rate

    def slash_for_missing_gradients(
        self, skipped_uids: list[int], success_rate: float
    ) -> None:
        """
        Slash peers that failed to submit gradients during gather.

        Args:
            skipped_uids: List of UIDs that were skipped during gather
            success_rate: Success rate of the gather operation
        """
        gather_peers_slash_rate = (
            0
            if success_rate < self.hparams.gather_peers_slash_threshold
            else self.missing_gradient_slash_rate
        )

        for uid in skipped_uids:
            tplr.log_with_context(
                level="info",
                message=f"No gradient gathered from UID {uid}. Slashing moving average score by {1 - gather_peers_slash_rate:.2%}.",
                sync_window=self.sync_window,
                current_window=self.current_window,
            )
            if 0 <= uid < self.final_scores.size(0):
                old_score = self.final_scores[uid].item()

                # Only reduce positive scores
                if self.final_scores[uid] > 0:
                    self.final_scores[uid] *= gather_peers_slash_rate
                    self.binary_moving_averages[uid] *= gather_peers_slash_rate

                    new_score = self.final_scores[uid].item()
                    tplr.log_with_context(
                        level="info",
                        message=f"Reduced score of UID {uid} from {old_score:.4f} to {new_score:.4f} due to missing gradient in gather.",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                    )
                else:
                    tplr.log_with_context(
                        level="info",
                        message=f"Skipped score of UID {uid} (current score: {old_score:.4f}) due to negative or zero value.",
                        sync_window=self.sync_window,
                        current_window=self.current_window,
                    )
                self.evaluated_uids.add(uid)
                self.peers_last_eval_window[uid] = self.sync_window
            else:
                tplr.log_with_context(
                    level="info",
                    message=f"UID {uid} not found in final_scores; skipping penalty.",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

    def slash_from_overlap(self, idx_overlap: dict) -> None:
        """
        Anyone with overly similar gradients is slashed; those
        with particularly egregious levels of overlap are 100%
        slashed. When 100% overlap, sent to timeout corner

        Args:
            idx_overlap: The overlap dictionary from tplr.neurons
        """
        idx_overlap_peers = {uid: "max" for uid in self.naughty_peers}
        idx_overlap_peers.update(idx_overlap.get("uids_over_thresh", {}))

        idx_overlap_peers = self.check_deregistered_uids(idx_overlap_peers)

        for uid, level in idx_overlap_peers.items():
            old_score = self.final_scores[uid].item()
            slash_multiplier = self.idx_similarity_slashing_rate.get(level, 0.5)

            if level in ["mega", "max"]:
                # For the most egregious offenders, reset scores
                self.reset_peer(uid)

                if level == "mega":
                    self.naughty_peers[uid] = self.naughty_peer_timeout

            if self.naughty_peers.get(uid):
                self.naughty_peers[uid] -= 1
                if self.naughty_peers[uid] <= 0:
                    self.naughty_peers.pop(uid)

            # Only reduce positive scores
            if old_score > 0:
                self.final_scores[uid] *= slash_multiplier
                self.binary_moving_averages[uid] *= slash_multiplier

                new_score = self.final_scores[uid].item()
                tplr.log_with_context(
                    level="info",
                    message=f"Reduced score of UID {uid} from {old_score:.4f} to {new_score:.4f} due to similarity in idxs.",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

            else:
                tplr.log_with_context(
                    level="info",
                    message=f"Skipped score of UID {uid} (current score: {old_score:.4f}) due to negative or zero value.",
                    sync_window=self.sync_window,
                    current_window=self.current_window,
                )

        return


def min_power_normalization(logits, power=2.0, epsilon=1e-8):
    """Normalizes logits using a minimum power normalization approach.

    This function applies power normalization to the input logits, raising them to a power
    and normalizing to create a probability distribution. If the sum is too small (below epsilon),
    returns zeros to avoid division by very small numbers.

    Args:
        logits (torch.Tensor): Input tensor to be normalized
        power (float, optional): Power to raise the logits to. Defaults to 2.0.
        epsilon (float, optional): Small value to prevent division by zero. Defaults to 1e-8.

    Returns:
        torch.Tensor: Normalized probabilities
    """
    if logits.dim() == 0:
        logits = logits.unsqueeze(0)

    powered_logits = logits**power
    sum_powered = torch.sum(powered_logits)
    if sum_powered > epsilon:
        probabilities = powered_logits / sum_powered
    else:
        probabilities = torch.zeros_like(powered_logits)

    return probabilities


if __name__ == "__main__":
    uvloop.install()
    try:
        asyncio.run(Validator().main())
    except KeyboardInterrupt:
        pass
