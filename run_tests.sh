#!/bin/bash
# Test runner script for Parsely

set -e

echo "=== Parsely Test Suite ==="
echo ""

# Check if pytest is installed
if ! command -v pytest &> /dev/null; then
    echo "pytest not found. Installing test dependencies..."
    pip install pytest pytest-cov
fi

# Parse arguments
COVERAGE=false
MARKERS=""
VERBOSE="-v"

while [[ $# -gt 0 ]]; do
    case $1 in
        --coverage|-c)
            COVERAGE=true
            shift
            ;;
        --unit|-u)
            MARKERS="-m unit"
            shift
            ;;
        --integration|-i)
            MARKERS="-m integration"
            shift
            ;;
        --api|-a)
            MARKERS="-m api"
            shift
            ;;
        --slow|-s)
            MARKERS="-m slow"
            shift
            ;;
        --all)
            MARKERS=""
            shift
            ;;
        -q|--quiet)
            VERBOSE="-q"
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--coverage] [--unit|--integration|--api|--slow|--all] [-q]"
            exit 1
            ;;
    esac
done

# Run tests
if [ "$COVERAGE" = true ]; then
    echo "Running tests with coverage..."
    pytest $VERBOSE $MARKERS --cov=pipeline --cov=dashboard --cov-report=term-missing --cov-report=html
    echo ""
    echo "Coverage report generated: htmlcov/index.html"
else
    echo "Running tests..."
    pytest $VERBOSE $MARKERS
fi

echo ""
echo "=== Tests Complete ==="
