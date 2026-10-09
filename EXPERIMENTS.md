# Experiments (criteria are written BEFORE the runs)

## E1. temperature 0 instead of 0.2 (DeepSeek, golden) - DONE
Success: stability >= 0.95 and mean verdict >= 0.850. Failure: stability < 0.93 or mean verdict < 0.850.
Result: stability 0.938, verdict 0.862 -> in between, not adopted. Errors that were noise became fixed (6 -> 10 always wrong).

## E2. prompt v2_locale: the separator rule depends on the locale (prompts/v2_locale.txt)
Change: ONE rule replaced ("Support both , and ; as separators"). Separators and the decimal comma now depend on the
regional settings; if the context names or implies the locale, or says the entry was rejected, check the separators
against it; if the locale is unknown, accept either when used consistently. Nothing else differs from v1_node_parity.
Hypothesis: the old rule makes the model accept a wrong separator in a known locale (g077, v010) and not know that ';'
goes with a decimal comma (g078 may be this too). NOT a hypothesis for g075 (missing leading '=').
Caveat: g077 and v010 were seen failing before the rule was written, so their gain is partly adaptive; the guard metrics matter.
Runs: DeepSeek, T=0.2, 3 runs on golden (all 80) + 3 runs on validation. Controls = existing runs 9-11 (golden) and 15, 17, 18 (validation).
Adopt only if ALL hold:
 1. Target: the verdict on the locale group, golden + validation together (22 cases x 3 runs), is higher than the control mean;
    and g077 and v010 are right in >= 2 of 3 runs.
 2. Guards: mean verdict on golden >= 0.850 and on validation >= 0.771 (the lowest control run of each).
 3. Regressions: cases right in >= 2/3 control runs and wrong in >= 2/3 new runs are listed; more than 2 in total, or any
    in the 'ok' category flipping to broken in a cluster, = reject.
 4. Qwen (3 runs on validation, 3 on golden, on separate days because of the Groq daily token limit) must not contradict:
    no guard below its control minimum (golden 0.725, validation 0.629).
Report both directions, including what got worse.

### Order of runs (rule for every prompt experiment)
First check that the change WORKS (the target, criterion 1) on the cheapest set that contains the target (usually
validation, 3 runs). If the target is not reached the change is rejected and the guard runs (golden, criteria 2-3) are NOT run.
Only if the target is reached do we check that nothing else broke: the full golden set, then Qwen.
Reason: guard runs cost more time, and spending them on a change that does not cure the target is pointless.

### E2 - RESULT: NOT ADOPTED (by the criteria written in advance)
Use run ids explicitly: golden --run 25 26 27 (the label deepseek-v2locale also holds a 16-case orphan run, ignore it);
validation --run 22 23 24.
1. Target: the locale group is better (golden +3 case-runs, validation 0.917 against 0.861; v010 0 -> 3, g078 0 -> 2,
   g072 2 -> 3), BUT g077 is not cured: NOT met.
2. Guards: golden 0.892 (control 0.879), validation 0.781 (control 0.781): met, the difference is within noise.
3. Regressions: g015 (3 -> 0), v019 (3 -> 1) = 2, at the limit. Improvement among 'ok' cases: g060 (1 -> 3). No false ok -> broken.
4. Qwen: not run (target not reached).
Reasons: g077 - the model misreads the formula (says the arguments are separated by ';' although the formula uses commas);
g015 - after the change the model treats dots in dates as "just a display matter" (hypothesis: stronger attention to
locale -> extra 'ok' answers).
Conclusion: rewriting a general rule gave about 1 point, swapping the model gave about 14; the locale rule is not iterated further.
