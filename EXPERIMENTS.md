# Experiments (criteria are written BEFORE the runs)

## E1. temperature 0 instead of 0.2 (DeepSeek, golden) - DONE
Success: stability >= 0.95 and mean verdict >= 0.850. Failure: stability < 0.93 or mean verdict < 0.850.
Result: stability 0.938, verdict 0.862 -> in between, not adopted. Errors that were noise became fixed (6 -> 10 always wrong).

## E2. prompt v2_locale: separator rule depends on the locale (prompts/v2_locale.txt)
Change: ONE rule replaced ("Support both , and ; as separators"): separators and the decimal comma now depend on the
regional settings; if the context names/implies the locale or says the entry was rejected, check separators against it;
if the locale is unknown, accept either when consistent. Nothing else differs from v1_node_parity.
Hypothesis: the old rule makes the model accept a wrong separator in a known locale (g077, v010) and not know that ';'
goes with a decimal comma (g078 may be this too). NOT a hypothesis for g075 (missing leading '=').
Caveat: g077/v010 were seen failing before the rule was written, so their gain is partly adaptive; the guard metrics matter.
Runs: DeepSeek, T=0.2, 3 runs on golden (all 80) + 3 runs on validation. Controls = existing runs 9-11 (golden) and 15,17,18 (validation).
Adopt only if ALL hold:
 1. Target: locale group verdict, golden+validation together (22 cases x 3 runs), is higher than the control mean;
    and g077 and v010 are right in >= 2 of 3 runs.
 2. Guards: mean verdict on golden >= 0.850 and on validation >= 0.771 (the lowest control run of each).
 3. Regressions: cases right in >= 2/3 control runs and wrong in >= 2/3 new runs are listed; more than 2 in total, or any
    in the 'ok' category flipping to broken in a cluster, = reject.
 4. Qwen (3 runs on validation, 3 on golden, on separate days because of the Groq daily token limit) must not contradict:
    no guard below its control minimum (golden 0.725, validation 0.629).
Report both directions, including what got worse.

### Порядок запуска (правило для всех экспериментов с промптом)
Сначала смотрим, РАБОТАЕТ ли правка (цель, критерий 1), на самом дешёвом наборе, где цель есть (обычно validation,
3 прогона). Если цель не достигнута: правка отвергнута, защитные прогоны (golden, критерии 2-3) НЕ запускаем.
Только если цель достигнута, проверяем, не СЛОМАЛОСЬ ли что-то другое: полный golden, затем Qwen.
Причина: защитные прогоны дороже по времени, и тратить их на правку, которая не лечит цель, нет смысла.


### E2 - РЕЗУЛЬТАТ: НЕ ПРИНЯТО (по заранее записанным критериям)
Использовать прогоны явно: golden --run 25 26 27 (в метке deepseek-v2locale есть ещё обрывок на 16 кейсов, его игнорировать);
validation --run 22 23 24.
1. Цель: locale лучше (golden +3 кейсо-прогона, validation 0.917 против 0.861; v010 0->3, g078 0->2, g072 2->3), НО g077 не вылечен: НЕ выполнено.
2. Защита: golden 0.892 (контроль 0.879), validation 0.781 (контроль 0.781): выполнено, разница в пределах шума.
3. Регрессии: g015 (3->0), v019 (3->1) = 2, на пределе. Улучшений ok-кейсов: g060 (1->3). Ложных ok->broken нет.
4. Qwen: не запускался (цель не достигнута).
Причины: g077 - модель неверно читает формулу (пишет, что аргументы разделены ';', хотя в формуле запятые); g015 -
после правки модель считает точки в датах 'просто отображением' (гипотеза: усиленное внимание к локали -> лишние 'ok').
Вывод: правка общего правила дала ~1 п.п., замена модели дала ~14 п.п.; дальше не итерируем правило про локаль.
