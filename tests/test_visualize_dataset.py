#!/usr/bin/env python

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from pathlib import Path

import pytest

from lerobot.scripts.visualize_dataset import visualize_dataset


@pytest.mark.parametrize(
    "repo_id",
    ["lerobot/pusht"],
)
@pytest.mark.parametrize("root", [Path(__file__).parent / "data"])
def test_visualize_dataset_root(tmpdir, repo_id, root):
    rrd_path = visualize_dataset(
        repo_id,
        episode_index=0,
        batch_size=32,
        save=True,
        root=root,
        output_dir=tmpdir,
    )
    assert rrd_path.exists()
