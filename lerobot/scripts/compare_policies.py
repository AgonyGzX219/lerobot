"""Compare two policies on based on metrics computed from an eval.

Usage example:

You just made changes to a policy and you want to assess its new performance against
the reference policy (i.e. before your changes).

```
python lerobot/scripts/compare_policies.py \
    output/eval/ref_policy/eval_info.json \
    output/eval/new_policy/eval_info.json
```

This script can accept `eval_info.json` dicts with identical seeds between each eval episode of ref_policy and
new_policy (paired-samples) or from evals performed with different seeds (independent samples).

The script will first perform normality tests to determine if parametric tests can be used or not, then
evaluate if policies metrics are significantly different using the appropriate tests.

CAVEATS: by default, this script will compare seeds numbers to determine if samples can be considered paired.
If changes have been made to this environment in-between the ref_policy eval and the new_policy eval, you
should use the `--independent` flag to override this and not pair the samples even if they have identical
seeds.
"""

import argparse
import json
import logging
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import scipy.stats as stats
from scipy.stats import anderson, kstest, mannwhitneyu, normaltest, shapiro, ttest_ind, ttest_rel, wilcoxon
from statsmodels.stats.contingency_tables import mcnemar
from termcolor import colored
from terminaltables import AsciiTable


def init_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[logging.StreamHandler()],
    )
    logging.getLogger("matplotlib.font_manager").disabled = True


def log_section(title: str) -> None:
    section_title = f"\n{'-'*21}\n {title.center(19)} \n{'-'*21}"
    logging.info(section_title)


def log_test(msg: str, p_value: float):
    if p_value < 0.01:
        color, interpretation = "red", "H_0 Rejected"
    elif 0.01 <= p_value < 0.05:
        color, interpretation = "yellow", "Inconclusive"
    else:
        color, interpretation = "green", "H_0 Not Rejected"
    logging.info(
        f"{msg}, p-value = {colored(f'{p_value:.3f}', color)} -> {colored(f'{interpretation}', color, attrs=['bold'])}"
    )


def get_eval_info_episodes(eval_info_path: Path) -> dict:
    with open(eval_info_path) as f:
        eval_info = json.load(f)

    return {
        "sum_rewards": np.array([ep_stat["sum_reward"] for ep_stat in eval_info["per_episode"]]),
        "max_rewards": np.array([ep_stat["max_reward"] for ep_stat in eval_info["per_episode"]]),
        "successes": np.array([ep_stat["success"] for ep_stat in eval_info["per_episode"]]),
        "seeds": [ep_stat["seed"] for ep_stat in eval_info["per_episode"]],
        "num_episodes": len(eval_info["per_episode"]),
    }


def append_table_metric(table: list, metric: str, ref_sample: dict, new_sample: dict, mean_std: bool = False):
    if mean_std:
        ref_metric = f"{np.mean(ref_sample[metric]):.3f} ({np.std(ref_sample[metric]):.3f})"
        new_metric = f"{np.mean(new_sample[metric]):.3f} ({np.std(new_sample[metric]):.3f})"
        row_header = f"{metric} - mean (std)"
    else:
        ref_metric = ref_sample[metric]
        new_metric = new_sample[metric]
        row_header = metric

    row = [row_header, ref_metric, new_metric]
    table.append(row)
    return table


def cohens_d(x, y):
    return (np.mean(x) - np.mean(y)) / np.sqrt((np.std(x, ddof=1) ** 2 + np.std(y, ddof=1) ** 2) / 2)


def normality_tests(array: np.ndarray, name: str):
    ap_stat, ap_p = normaltest(array)
    sw_stat, sw_p = shapiro(array)
    ks_stat, ks_p = kstest(array, "norm", args=(np.mean(array), np.std(array)))
    ad_stat = anderson(array)

    log_test(f"{name} - D'Agostino and Pearson test: statistic = {ap_stat:.3f}", ap_p)
    log_test(f"{name} - Shapiro-Wilk test: statistic = {sw_stat:.3f}", sw_p)
    log_test(f"{name} - Kolmogorov-Smirnov test: statistic = {ks_stat:.3f}", ks_p)
    logging.info(f"{name} - Anderson-Darling test: statistic = {ad_stat.statistic:.3f}")
    for i in range(len(ad_stat.critical_values)):
        cv, sl = ad_stat.critical_values[i], ad_stat.significance_level[i]
        logging.info(f"    Critical value at {sl}%: {cv:.3f}")

    return sw_p > 0.05 and ks_p > 0.05


def perform_tests(ref_sample: dict, new_sample: dict, output_dir: Path, independent: bool = False):
    seeds_a, seeds_b = ref_sample["seeds"], new_sample["seeds"]
    if (seeds_a == seeds_b) and not independent:
        logging.info("\nSamples are paired (identical seeds).")
        paired = True
    else:
        logging.info("\nSamples are considered independent (seeds are different).")
        paired = False

    table_data = [["Metric", "Ref.", "New"]]
    table_data = append_table_metric(table_data, "num_episodes", ref_sample, new_sample)
    table_data = append_table_metric(table_data, "successes", ref_sample, new_sample, mean_std=True)
    table_data = append_table_metric(table_data, "max_rewards", ref_sample, new_sample, mean_std=True)
    table_data = append_table_metric(table_data, "sum_rewards", ref_sample, new_sample, mean_std=True)
    table = AsciiTable(table_data)
    print(table.table)

    log_section("Effect Size")
    d_max_reward = cohens_d(ref_sample["max_rewards"], new_sample["max_rewards"])
    d_sum_reward = cohens_d(ref_sample["sum_rewards"], new_sample["sum_rewards"])
    logging.info(f"Cohen's d for Max Reward: {d_max_reward:.3f}")
    logging.info(f"Cohen's d for Sum Reward: {d_sum_reward:.3f}")

    if paired:
        paired_sample_tests(ref_sample, new_sample)
    else:
        independent_sample_tests(ref_sample, new_sample)

    output_dir.mkdir(exist_ok=True, parents=True)

    plot_boxplot(
        ref_sample["max_rewards"],
        new_sample["max_rewards"],
        ["Ref Sample Max Reward", "New Sample Max Reward"],
        "Boxplot of Max Rewards",
        f"{output_dir}/boxplot_max_reward.png",
    )
    plot_boxplot(
        ref_sample["sum_rewards"],
        new_sample["sum_rewards"],
        ["Ref Sample Sum Reward", "New Sample Sum Reward"],
        "Boxplot of Sum Rewards",
        f"{output_dir}/boxplot_sum_reward.png",
    )

    plot_histogram(
        ref_sample["max_rewards"],
        new_sample["max_rewards"],
        ["Ref Sample Max Reward", "New Sample Max Reward"],
        "Histogram of Max Rewards",
        f"{output_dir}/histogram_max_reward.png",
    )
    plot_histogram(
        ref_sample["sum_rewards"],
        new_sample["sum_rewards"],
        ["Ref Sample Sum Reward", "New Sample Sum Reward"],
        "Histogram of Sum Rewards",
        f"{output_dir}/histogram_sum_reward.png",
    )

    plot_qqplot(
        ref_sample["max_rewards"],
        "Q-Q Plot of Ref Sample Max Rewards",
        f"{output_dir}/qqplot_sample_a_max_reward.png",
    )
    plot_qqplot(
        new_sample["max_rewards"],
        "Q-Q Plot of New Sample Max Rewards",
        f"{output_dir}/qqplot_sample_b_max_reward.png",
    )
    plot_qqplot(
        ref_sample["sum_rewards"],
        "Q-Q Plot of Ref Sample Sum Rewards",
        f"{output_dir}/qqplot_sample_a_sum_reward.png",
    )
    plot_qqplot(
        new_sample["sum_rewards"],
        "Q-Q Plot of New Sample Sum Rewards",
        f"{output_dir}/qqplot_sample_b_sum_reward.png",
    )


def paired_sample_tests(ref_sample: dict, new_sample: dict):
    log_section("Normality tests")
    max_reward_diff = ref_sample["max_rewards"] - new_sample["max_rewards"]
    sum_reward_diff = ref_sample["sum_rewards"] - new_sample["sum_rewards"]

    normal_max_reward_diff = normality_tests(max_reward_diff, "Max Reward Difference")
    normal_sum_reward_diff = normality_tests(sum_reward_diff, "Sum Reward Difference")

    log_section("Paired-sample tests")
    if normal_max_reward_diff:
        t_stat_max_reward, p_val_max_reward = ttest_rel(ref_sample["max_rewards"], new_sample["max_rewards"])
        log_test(f"Paired t-test for Max Reward: t-statistic = {t_stat_max_reward:.3f}", p_val_max_reward)
    else:
        w_stat_max_reward, p_wilcox_max_reward = wilcoxon(
            ref_sample["max_rewards"], new_sample["max_rewards"]
        )
        log_test(f"Wilcoxon test for Max Reward: statistic = {w_stat_max_reward:.3f}", p_wilcox_max_reward)

    if normal_sum_reward_diff:
        t_stat_sum_reward, p_val_sum_reward = ttest_rel(ref_sample["sum_rewards"], new_sample["sum_rewards"])
        log_test(f"Paired t-test for Sum Reward: t-statistic = {t_stat_sum_reward:.3f}", p_val_sum_reward)
    else:
        w_stat_sum_reward, p_wilcox_sum_reward = wilcoxon(
            ref_sample["sum_rewards"], new_sample["sum_rewards"]
        )
        log_test(f"Wilcoxon test for Sum Reward: statistic = {w_stat_sum_reward:.3f}", p_wilcox_sum_reward)

    table = np.array(
        [
            [
                np.sum((ref_sample["successes"] == 1) & (new_sample["successes"] == 1)),
                np.sum((ref_sample["successes"] == 1) & (new_sample["successes"] == 0)),
            ],
            [
                np.sum((ref_sample["successes"] == 0) & (new_sample["successes"] == 1)),
                np.sum((ref_sample["successes"] == 0) & (new_sample["successes"] == 0)),
            ],
        ]
    )
    mcnemar_result = mcnemar(table, exact=True)
    log_test(f"McNemar's test for Success: statistic = {mcnemar_result.statistic:.3f}", mcnemar_result.pvalue)


def independent_sample_tests(ref_sample: dict, new_sample: dict):
    log_section("Normality tests")
    normal_max_rewards_a = normality_tests(ref_sample["max_rewards"], "Max Rewards Ref Sample")
    normal_max_rewards_b = normality_tests(new_sample["max_rewards"], "Max Rewards New Sample")
    normal_sum_rewards_a = normality_tests(ref_sample["sum_rewards"], "Sum Rewards Ref Sample")
    normal_sum_rewards_b = normality_tests(new_sample["sum_rewards"], "Sum Rewards New Sample")

    log_section("Independent samples tests")
    table = [["Test", "max_rewards", "sum_rewards"]]
    if normal_max_rewards_a and normal_max_rewards_b:
        table = append_independent_test(
            table, ref_sample, new_sample, ttest_ind, "Two-Sample t-test", kwargs={"equal_var": False}
        )
        t_stat_max_reward, p_val_max_reward = ttest_ind(
            ref_sample["max_rewards"], new_sample["max_rewards"], equal_var=False
        )
        log_test(f"Two-Sample t-test for Max Reward: t-statistic = {t_stat_max_reward:.3f}", p_val_max_reward)
    else:
        table = append_independent_test(table, ref_sample, new_sample, mannwhitneyu, "Mann-Whitney U")
        u_stat_max_reward, p_u_max_reward = mannwhitneyu(ref_sample["max_rewards"], new_sample["max_rewards"])
        log_test(f"Mann-Whitney U test for Max Reward: U-statistic = {u_stat_max_reward:.3f}", p_u_max_reward)

    if normal_sum_rewards_a and normal_sum_rewards_b:
        t_stat_sum_reward, p_val_sum_reward = ttest_ind(
            ref_sample["sum_rewards"], new_sample["sum_rewards"], equal_var=False
        )
        log_test(f"Two-Sample t-test for Sum Reward: t-statistic = {t_stat_sum_reward:.3f}", p_val_sum_reward)
    else:
        u_stat_sum_reward, p_u_sum_reward = mannwhitneyu(ref_sample["sum_rewards"], new_sample["sum_rewards"])
        log_test(f"Mann-Whitney U test for Sum Reward: U-statistic = {u_stat_sum_reward:.3f}", p_u_sum_reward)

    table = AsciiTable(table)
    print(table.table)


def append_independent_test(
    table: list,
    ref_sample: dict,
    new_sample: dict,
    test: callable,
    test_name: str,
    kwargs: dict | None = None,
) -> list:
    kwargs = {} if kwargs is None else kwargs
    row = [f"{test_name}: p-value ≥ alpha"]
    for metric in table[0][1:]:
        _, p_val = test(ref_sample[metric], new_sample[metric], **kwargs)
        alpha = 0.05
        status = "✅" if p_val >= alpha else "❌"
        row.append(f"{status} {p_val:.3f} ≥ {alpha}")

    table.append(row)
    return table


def plot_boxplot(data_a: np.ndarray, data_b: np.ndarray, labels: list[str], title: str, filename: str):
    plt.boxplot([data_a, data_b], labels=labels)
    plt.title(title)
    plt.savefig(filename)
    plt.close()


def plot_histogram(data_a: np.ndarray, data_b: np.ndarray, labels: list[str], title: str, filename: str):
    plt.hist(data_a, bins=30, alpha=0.7, label=labels[0])
    plt.hist(data_b, bins=30, alpha=0.7, label=labels[1])
    plt.title(title)
    plt.legend()
    plt.savefig(filename)
    plt.close()


def plot_qqplot(data: np.ndarray, title: str, filename: str):
    stats.probplot(data, dist="norm", plot=plt)
    plt.title(title)
    plt.savefig(filename)
    plt.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("ref_sample_path", type=Path, help="Path to the reference sample JSON file.")
    parser.add_argument("new_sample_path", type=Path, help="Path to the new sample JSON file.")
    parser.add_argument(
        "--independent",
        action="store_true",
        help="Ignore seeds and consider samples to be independent (unpaired).",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=Path("outputs/compare/"),
        help="Directory to save the output results. Defaults to outputs/compare/",
    )
    args = parser.parse_args()
    init_logging()

    ref_sample = get_eval_info_episodes(args.ref_sample_path)
    new_sample = get_eval_info_episodes(args.new_sample_path)
    perform_tests(ref_sample, new_sample, args.output_dir, args.independent)
