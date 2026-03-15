#!/usr/bin/env python3
"""
invoke_local.py — CLI helper for running the CloudOps AI Agent locally.

Usage
-----
python scripts/invoke_local.py \
    --description "Lambda payments-processor has 40% error rate" \
    --resources '{"lambda": ["payments-processor"], "rds": ["prod-db"]}' \
    --severity HIGH \
    --output pretty

python scripts/invoke_local.py --help
"""

import argparse
import json
import os
import sys
import traceback

# Allow running from project root OR scripts/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional

from app import run_local


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_INCIDENTS = {
    "lambda-errors": (
        "CRITICAL: Lambda function payments-processor has a 40% error rate "
        "since 14:00 UTC. Users are experiencing failed transactions and "
        "getting 500 errors. Some requests are also timing out.",
        {"lambda": ["payments-processor"], "rds": ["prod-payments-db"]},
        "CRITICAL",
    ),
    "ec2-cpu": (
        "HIGH: EC2 instance i-0abc123def456789 has CPU utilization above 90% "
        "for the last 45 minutes. Application latency has increased significantly.",
        {"ec2": ["i-0abc123def456789"]},
        "HIGH",
    ),
    "rds-connections": (
        "HIGH: RDS instance prod-db is reporting max connection errors. "
        "Applications are failing to connect to the database.",
        {"rds": ["prod-db"], "lambda": ["api-handler"]},
        "HIGH",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog       = "invoke_local",
        description = "Run the CloudOps AI Agent pipeline locally",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
examples:
  # Use a built-in sample incident
  python scripts/invoke_local.py --sample lambda-errors

  # Custom incident
  python scripts/invoke_local.py \\
    --description "Lambda has 30% errors" \\
    --resources '{"lambda": ["my-function"]}' \\
    --severity HIGH

  # Compact JSON output
  python scripts/invoke_local.py --sample ec2-cpu --output compact
        """,
    )
    parser.add_argument(
        "--description", "-d",
        type    = str,
        default = None,
        help    = "Free-form incident description",
    )
    parser.add_argument(
        "--resources", "-r",
        type    = str,
        default = "{}",
        help    = 'JSON dict of resource IDs, e.g. \'{"lambda": ["fn-name"]}\'',
    )
    parser.add_argument(
        "--severity", "-s",
        type    = str,
        choices = ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
        default = None,
        help    = "Override severity level",
    )
    parser.add_argument(
        "--sample",
        type    = str,
        choices = list(SAMPLE_INCIDENTS.keys()),
        default = None,
        help    = "Use a built-in sample incident",
    )
    parser.add_argument(
        "--output", "-o",
        type    = str,
        choices = ["pretty", "compact", "summary"],
        default = "pretty",
        help    = "Output format (default: pretty)",
    )
    return parser.parse_args()


def print_summary(result: dict) -> None:
    """Print a human-readable summary instead of raw JSON."""
    pipeline = result.get("pipeline_result", {})
    perf     = result.get("performance", {})

    bar = "═" * 60
    print(f"\n{bar}")
    print(f"  CloudOps AI Agent — Pipeline Result")
    print(bar)
    print(f"  Status      : {result.get('status', '?')}")
    print(f"  Incident ID : {result.get('incident_id', '?')}")
    print(f"  Severity    : {result.get('severity', '?')}")
    print(f"  Health      : {pipeline.get('overall_health', '?')}")
    print(f"  Est. TTR    : {pipeline.get('estimated_ttr_minutes', '?')} min")
    print(f"  Elapsed     : {perf.get('total_elapsed_seconds', '?')}s")
    print(bar)

    stages = ["incident_summary", "metrics_summary", "log_summary", "remediation_summary"]
    labels = ["Incident",         "Metrics",          "Logs",        "Remediation"]
    for stage, label in zip(stages, labels):
        val = pipeline.get(stage, "N/A")
        if val:
            print(f"\n  [{label}]")
            # Wrap long lines
            for line in str(val).split(". "):
                if line:
                    print(f"    {line.strip()}.")

    final_rec = pipeline.get("final_recommendation")
    if final_rec:
        try:
            rec = json.loads(final_rec)
            print(f"\n  [Final Recommendation]")
            print(f"    {rec.get('executive_summary', '')}")
            print(f"\n  [Recommended Action]")
            print(f"    {rec.get('recommended_action', '')}")
            if rec.get("post_incident_tasks"):
                print(f"\n  [Post-Incident Tasks]")
                for task in rec["post_incident_tasks"]:
                    print(f"    • {task}")
        except (json.JSONDecodeError, AttributeError):
            print(f"\n  [Final Recommendation]\n    {final_rec}")

    print(f"\n{bar}\n")


def main() -> None:
    args = parse_args()

    # Resolve incident parameters
    if args.sample:
        description, resource_hints, severity = SAMPLE_INCIDENTS[args.sample]
        if args.severity:
            severity = args.severity
        print(f"Using sample: {args.sample}")
    else:
        if not args.description:
            print("ERROR: --description or --sample is required.")
            sys.exit(1)
        description = args.description
        severity    = args.severity
        try:
            resource_hints = json.loads(args.resources)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Invalid JSON for --resources: {exc}")
            sys.exit(1)

    print(f"\nStarting pipeline...")
    print(f"  Description : {description[:80]}{'...' if len(description) > 80 else ''}")
    print(f"  Resources   : {resource_hints}")
    print(f"  Severity    : {severity or 'auto-detect'}")
    print()

    try:
        result = run_local(
            incident_description = description,
            resource_hints       = resource_hints,
            override_severity    = severity,
            pretty_print         = (args.output == "pretty"),
        )

        if args.output == "compact":
            print(json.dumps(result, default=str))
        elif args.output == "summary":
            print_summary(result)
        # "pretty" already handled by run_local(pretty_print=True)

        sys.exit(0 if result.get("status") == "SUCCESS" else 1)

    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nFATAL ERROR: {exc}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
