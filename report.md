# Equivalence Test Report
**Generated:** 2026-05-25T10:51:19.367902

## Decision: APPROVED
The migration is approved with an equivalence rate of 100%.

## Metrics
| Metric | Value |
|---|---|
| Equivalence rate | 100% |
| Valid baseline (tests passing on original) | 1 |
| Regressions detected | 0 |
| Symmetric failures (generation noise) | 6 |
| Coverage (original) | 95.65% |
| Coverage (migrated) | 68.42% |

## Regressions
None detected

## Symmetric Failures (ignored in scoring)
* test_equivalence.py::test_invalid_input
* test_equivalence.py::test_happy_path
* test_equivalence.py::test_network_error
* test_equivalence.py::test_http_error
* test_equivalence.py::test_timeout_error
* test_equivalence.py::test_empty_recaptcha_response

## Warnings
None