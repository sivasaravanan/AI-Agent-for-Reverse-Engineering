"""Stage B: education features - batch grading of student submissions, similarity review, rubric scoring, reports.

Static only: submissions are read in memory (ZIP / tar / folder) and never executed. Similarity output is a lead for
human review, never a finding of misconduct. Scores cover only what static analysis can see; correctness that needs
running the code is left to manual criteria.
"""
from education.rubric import Rubric, RubricError, default_rubric, load_rubric
from education.similarity import SimilarityOptions, compare_submissions
from education.grader import BatchResult, StudentGrade, discover_submissions, grade_batch, load_manual_scores

__all__ = ["Rubric", "RubricError", "default_rubric", "load_rubric", "SimilarityOptions", "compare_submissions",
           "BatchResult", "StudentGrade", "discover_submissions", "grade_batch", "load_manual_scores"]
