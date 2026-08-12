from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Task 1 — Data models
# ---------------------------------------------------------------------------

@dataclass
class QAPair:
    question: str
    expected_answer: str
    context: str = ""
    metadata: dict = field(default_factory=dict)
    retrieved_contexts: list[str] = field(default_factory=list)


@dataclass
class EvalResult:
    qa_pair: QAPair
    actual_answer: str
    faithfulness: float
    relevance: float
    completeness: float
    passed: bool
    failure_type: str | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    def overall_score(self) -> float:
        return (
            self.faithfulness
            + self.relevance
            + self.completeness
        ) / 3.0


# ---------------------------------------------------------------------------
# Task 2 — Simplified RAGAS-style evaluator
# ---------------------------------------------------------------------------

STOPWORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "as", "by", "and", "or",
    "it", "its", "this", "that", "these", "those", "from", "into", "than",
}


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    tokens = re.findall(r"\b\w+\b", text.lower())
    return {token for token in tokens if token not in STOPWORDS}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


class RAGASEvaluator:
    def evaluate_faithfulness(self, answer: str, context: str) -> float:
        answer_tokens = _tokenize(answer)
        if not answer_tokens:
            return 1.0

        context_tokens = _tokenize(context)
        score = len(answer_tokens & context_tokens) / len(answer_tokens)
        return _clamp01(score)

    def evaluate_relevance(self, answer: str, question: str) -> float:
        question_tokens = _tokenize(question)
        if not question_tokens:
            return 1.0

        answer_tokens = _tokenize(answer)
        score = len(answer_tokens & question_tokens) / len(question_tokens)
        return _clamp01(score)

    def evaluate_completeness(self, answer: str, expected: str) -> float:
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0

        answer_tokens = _tokenize(answer)
        score = len(answer_tokens & expected_tokens) / len(expected_tokens)
        return _clamp01(score)

    def evaluate_context_recall(
        self,
        contexts: list[str],
        expected: str,
    ) -> float:
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0

        union_tokens: set[str] = set()
        for chunk in contexts:
            union_tokens.update(_tokenize(chunk))

        score = len(expected_tokens & union_tokens) / len(expected_tokens)
        return _clamp01(score)

    def evaluate_context_precision(
        self,
        contexts: list[str],
        expected: str,
        relevance_threshold: float = 0.1,
    ) -> float:
        expected_tokens = _tokenize(expected)

        if not expected_tokens:
            return 1.0
        if not contexts:
            return 0.0

        relevance_flags: list[bool] = []

        for chunk in contexts:
            chunk_tokens = _tokenize(chunk)
            coverage = len(chunk_tokens & expected_tokens) / len(expected_tokens)
            relevance_flags.append(coverage >= relevance_threshold)

        total_relevant = sum(relevance_flags)
        if total_relevant == 0:
            return 0.0

        relevant_seen = 0
        precision_sum = 0.0

        for rank, is_relevant in enumerate(relevance_flags, start=1):
            if is_relevant:
                relevant_seen += 1
                precision_sum += relevant_seen / rank

        return _clamp01(precision_sum / total_relevant)

    def run_full_eval(
        self,
        answer: str,
        question: str,
        context: str,
        expected: str,
        contexts: list[str] | None = None,
    ) -> EvalResult:
        faithfulness = self.evaluate_faithfulness(answer, context)
        relevance = self.evaluate_relevance(answer, question)
        completeness = self.evaluate_completeness(answer, expected)

        passed = (
            faithfulness >= 0.5
            and relevance >= 0.5
            and completeness >= 0.5
        )

        failure_type: str | None = None
        if not passed:
            if faithfulness < 0.3:
                failure_type = "hallucination"
            elif relevance < 0.3:
                failure_type = "irrelevant"
            elif completeness < 0.3:
                failure_type = "incomplete"
            else:
                failure_type = "off_topic"

        context_recall: float | None = None
        context_precision: float | None = None

        if contexts is not None:
            context_recall = self.evaluate_context_recall(contexts, expected)
            context_precision = self.evaluate_context_precision(contexts, expected)

        qa_pair = QAPair(
            question=question,
            expected_answer=expected,
            context=context,
            retrieved_contexts=list(contexts) if contexts is not None else [],
        )

        return EvalResult(
            qa_pair=qa_pair,
            actual_answer=answer,
            faithfulness=faithfulness,
            relevance=relevance,
            completeness=completeness,
            passed=passed,
            failure_type=failure_type,
            context_precision=context_precision,
            context_recall=context_recall,
        )


# Bonus — Exercise 3.5.
# Leaving this unimplemented is expected for Part 2 and makes one test skip.
def rerank_by_overlap(contexts: list[str], query: str) -> list[str]:
    raise NotImplementedError("Bonus exercise 3.5 is not implemented")


# ---------------------------------------------------------------------------
# Task 3 — LLM-as-a-Judge
# ---------------------------------------------------------------------------

class LLMJudge:
    def __init__(self, judge_llm_fn: Callable[[str], str]) -> None:
        self.judge_llm_fn = judge_llm_fn

    def score_response(
        self,
        question: str,
        answer: str,
        rubric: dict[str, Any],
    ) -> dict[str, Any]:
        rubric_lines = "\n".join(
            f"- {criterion}: {description}"
            for criterion, description in rubric.items()
        )

        prompt = (
            "Evaluate the answer using the rubric below.\n"
            "Return JSON containing one numeric score in [0, 1] per criterion.\n\n"
            f"Question:\n{question}\n\n"
            f"Answer:\n{answer}\n\n"
            f"Rubric:\n{rubric_lines}\n"
        )

        raw_response = self.judge_llm_fn(prompt)

        default_scores = {criterion: 0.5 for criterion in rubric}

        try:
            parsed = json.loads(raw_response)
            source_scores = (
                parsed.get("scores", {})
                if isinstance(parsed, dict) and isinstance(parsed.get("scores"), dict)
                else parsed
            )

            if not isinstance(source_scores, dict):
                raise ValueError("Judge response must contain a JSON object")

            scores: dict[str, float] = {}
            for criterion in rubric:
                raw_score = source_scores.get(criterion, 0.5)
                try:
                    score = float(raw_score)
                except (TypeError, ValueError):
                    score = 0.5

                # Be tolerant of a judge returning the common 1–5 scale.
                if 1.0 < score <= 5.0:
                    score /= 5.0

                scores[criterion] = _clamp01(score)

        except (json.JSONDecodeError, TypeError, ValueError):
            scores = default_scores

        return {
            "scores": scores,
            "reasoning": raw_response,
        }

    def detect_bias(self, scores_batch: list[dict[str, Any]]) -> dict[str, Any]:
        per_response_averages: list[float] = []
        all_scores: list[float] = []

        for item in scores_batch:
            scores = item.get("scores", {})
            if not isinstance(scores, dict):
                continue

            numeric_scores: list[float] = []
            for value in scores.values():
                try:
                    numeric_scores.append(float(value))
                except (TypeError, ValueError):
                    continue

            if numeric_scores:
                per_response_averages.append(
                    sum(numeric_scores) / len(numeric_scores)
                )
                all_scores.extend(numeric_scores)

        if not all_scores:
            return {
                "positional_bias": False,
                "leniency_bias": False,
                "severity_bias": False,
            }

        overall_average = sum(all_scores) / len(all_scores)

        # With only aggregate sequential scores available, this is a simple
        # warning heuristic rather than a causal position-bias test.
        positional_bias = False
        if len(per_response_averages) >= 2:
            first = per_response_averages[0]
            later_average = (
                sum(per_response_averages[1:])
                / len(per_response_averages[1:])
            )
            positional_bias = first > later_average + 0.05

        return {
            "positional_bias": positional_bias,
            "leniency_bias": overall_average > 0.8,
            "severity_bias": overall_average < 0.3,
        }


# ---------------------------------------------------------------------------
# Task 4 — Benchmark runner
# ---------------------------------------------------------------------------

class BenchmarkRunner:
    def run(
        self,
        qa_pairs: list[QAPair],
        agent_fn: Callable[[str], str],
        evaluator: RAGASEvaluator,
    ) -> list[EvalResult]:
        results: list[EvalResult] = []

        for pair in qa_pairs:
            answer = agent_fn(pair.question)

            result = evaluator.run_full_eval(
                answer=answer,
                question=pair.question,
                context=pair.context,
                expected=pair.expected_answer,
                contexts=pair.retrieved_contexts,
            )

            # Preserve metadata and the exact original pair supplied by caller.
            result.qa_pair = pair
            results.append(result)

        return results

    def generate_report(self, results: list[EvalResult]) -> dict[str, Any]:
        total = len(results)

        if total == 0:
            return {
                "total": 0,
                "passed": 0,
                "pass_rate": 0.0,
                "avg_faithfulness": 0.0,
                "avg_relevance": 0.0,
                "avg_completeness": 0.0,
                "avg_context_recall": None,
                "avg_context_precision": None,
                "failure_types": {},
            }

        passed_count = sum(result.passed for result in results)

        failure_types: dict[str, int] = {}
        for result in results:
            if not result.passed and result.failure_type:
                failure_types[result.failure_type] = (
                    failure_types.get(result.failure_type, 0) + 1
                )

        recall_values = [
            result.context_recall
            for result in results
            if result.context_recall is not None
        ]
        precision_values = [
            result.context_precision
            for result in results
            if result.context_precision is not None
        ]

        return {
            "total": total,
            "passed": passed_count,
            "pass_rate": passed_count / total,
            "avg_faithfulness": (
                sum(result.faithfulness for result in results) / total
            ),
            "avg_relevance": (
                sum(result.relevance for result in results) / total
            ),
            "avg_completeness": (
                sum(result.completeness for result in results) / total
            ),
            "avg_context_recall": (
                sum(recall_values) / len(recall_values)
                if recall_values
                else None
            ),
            "avg_context_precision": (
                sum(precision_values) / len(precision_values)
                if precision_values
                else None
            ),
            "failure_types": failure_types,
        }

    def run_regression(
        self,
        new_results: list,
        baseline_results: list,
    ) -> dict:
        metric_names = ("faithfulness", "relevance", "completeness")

        def average(results: list, metric: str) -> float:
            if not results:
                return 0.0
            return sum(getattr(result, metric) for result in results) / len(results)

        new_avgs = {
            metric: average(new_results, metric)
            for metric in metric_names
        }
        baseline_avgs = {
            metric: average(baseline_results, metric)
            for metric in metric_names
        }

        regressions = [
            metric
            for metric in metric_names
            if baseline_avgs[metric] - new_avgs[metric] > 0.05
        ]

        return {
            "new_avg_faithfulness": new_avgs["faithfulness"],
            "new_avg_relevance": new_avgs["relevance"],
            "new_avg_completeness": new_avgs["completeness"],
            "baseline_avg_faithfulness": baseline_avgs["faithfulness"],
            "baseline_avg_relevance": baseline_avgs["relevance"],
            "baseline_avg_completeness": baseline_avgs["completeness"],
            "regressions": regressions,
            "passed": len(regressions) == 0,
        }

    def identify_failures(
        self,
        results: list[EvalResult],
        threshold: float = 0.5,
    ) -> list[EvalResult]:
        return [
            result
            for result in results
            if (
                result.faithfulness < threshold
                or result.relevance < threshold
                or result.completeness < threshold
            )
        ]


# ---------------------------------------------------------------------------
# Task 5 — Failure analysis
# ---------------------------------------------------------------------------

class FailureAnalyzer:
    def categorize_failures(
        self,
        failures: list[EvalResult],
    ) -> dict[str, int]:
        categories: dict[str, int] = {}

        for failure in failures:
            key = failure.failure_type or "unknown"
            categories[key] = categories.get(key, 0) + 1

        return categories

    def find_root_cause(self, failure: EvalResult) -> str:
        scores = {
            "faithfulness": failure.faithfulness,
            "relevance": failure.relevance,
            "completeness": failure.completeness,
        }

        minimum = min(scores.values())
        lowest = [
            metric
            for metric, score in scores.items()
            if score == minimum
        ]

        if len(lowest) != 1:
            return "Multiple issues detected — review full pipeline"

        if lowest[0] == "faithfulness":
            return "Context is missing or irrelevant — improve retrieval"
        if lowest[0] == "relevance":
            return "Answer does not address the question — improve prompt clarity"
        if lowest[0] == "completeness":
            return (
                "Answer is missing key information — "
                "increase context window or improve generation"
            )

        return "Multiple issues detected — review full pipeline"

    def generate_improvement_suggestions(
        self,
        failures: list[EvalResult],
    ) -> list[str]:
        if not failures:
            return []

        categories = self.categorize_failures(failures)
        suggestions: list[str] = []

        if categories.get("hallucination", 0):
            suggestions.append(
                "Strengthen grounding checks so unsupported claims are filtered "
                "or rewritten using retrieved evidence."
            )

        if categories.get("irrelevant", 0):
            suggestions.append(
                "Improve prompt and intent handling so answers address the "
                "user question directly."
            )

        if categories.get("incomplete", 0):
            suggestions.append(
                "Improve retrieval coverage and generation instructions so "
                "required conditions and details are not omitted."
            )

        if categories.get("off_topic", 0):
            suggestions.append(
                "Add clearer scope and intent-routing rules to reduce off-topic responses."
            )

        if categories.get("refusal", 0):
            suggestions.append(
                "Review refusal guardrails so valid in-scope questions are not rejected."
            )

        # Part 2 asks for at least three actionable ideas when failures exist.
        fallback_suggestions = [
            "Add regression tests for recurring failure patterns before deployment.",
            "Review low-scoring traces and expand the golden dataset with representative edge cases.",
            "Track retrieval and answer-side metrics separately to localize pipeline regressions.",
        ]

        for suggestion in fallback_suggestions:
            if len(suggestions) >= 3:
                break
            suggestions.append(suggestion)

        return suggestions

    def generate_improvement_log(
        self,
        failures: list,
        suggestions: list[str],
    ) -> str:
        header = (
            "| Failure ID | Type | Root Cause | Suggested Fix | Status |\n"
            "|------------|------|------------|---------------|--------|"
        )

        rows: list[str] = []

        for index, failure in enumerate(failures, start=1):
            failure_id = f"F{index:03d}"
            failure_type = failure.failure_type or "unknown"
            root_cause = self.find_root_cause(failure)

            if index - 1 < len(suggestions):
                suggestion = suggestions[index - 1]
            else:
                suggestion = "Review this failure and apply the root-cause fix"

            # Keep the Markdown table valid if text contains a pipe.
            failure_type = str(failure_type).replace("|", r"\|")
            root_cause = root_cause.replace("|", r"\|")
            suggestion = str(suggestion).replace("|", r"\|")

            rows.append(
                f"| {failure_id} | {failure_type} | {root_cause} | "
                f"{suggestion} | Open |"
            )

        return "\n".join([header, *rows])


if __name__ == "__main__":
    evaluator = RAGASEvaluator()
    runner = BenchmarkRunner()

    qa_pairs = [
        QAPair(
            question="What is the capital of France?",
            expected_answer="Paris is the capital of France.",
            context="Paris is the capital city of France.",
            retrieved_contexts=["Paris is the capital city of France."],
        )
    ]

    results = runner.run(
        qa_pairs,
        lambda _: "Paris is the capital of France.",
        evaluator,
    )

    print(runner.generate_report(results))