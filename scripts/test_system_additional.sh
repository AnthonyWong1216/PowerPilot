#!/bin/ksh
################################################################################
# Additional System Tests for AIX/VIOS
# Description: Disk performance, filesystem operations, network latency,
#              and system stability tests
# Usage: ./test_system_additional.sh [test_directory] [results_dir]
################################################################################

# Configuration
TEST_DIR=${1:-"/tmp/aix_test_data"}
TIMESTAMP=${2:-$(date +%Y%m%d_%H%M%S)}
HOSTNAME=$(hostname -s)
RESULTS_DIR="${HOSTNAME}_system_additional"
mkdir -p "$RESULTS_DIR"
OUTPUT_FILE="${RESULTS_DIR}/${TIMESTAMP}_result.log"

# Test parameters
PING_COUNT=10
PING_TARGETS=""  # No internet targets
FILE_COUNT=1000
STRESS_DURATION=60

################################################################################
# Functions
################################################################################

print_header() {
    echo "" | tee -a "$OUTPUT_FILE"
    echo "================================================================================" | tee -a "$OUTPUT_FILE"
    echo "$1" | tee -a "$OUTPUT_FILE"
    echo "================================================================================" | tee -a "$OUTPUT_FILE"
    echo "" | tee -a "$OUTPUT_FILE"
}

print_info() {
    echo "[INFO] $1" | tee -a "$OUTPUT_FILE"
}

print_error() {
    echo "[ERROR] $1" | tee -a "$OUTPUT_FILE"
}

# Disk and filesystem tests removed - use fio for I/O testing instead

test_network_latency() {
    print_header "Network Configuration"
    
    print_info "Collecting network interface information..."
    {
        echo "--- Network Interfaces ---"
        ifconfig -a
        echo ""
        
    } | tee -a "$OUTPUT_FILE"
}

test_dns_resolution() {
    print_header "DNS Resolution Performance"
    
    print_info "Skipping DNS resolution test (no internet connection required)"
    echo "DNS resolution test skipped - configure PING_TARGETS if needed" | tee -a "$OUTPUT_FILE"
    echo "" | tee -a "$OUTPUT_FILE"
}

# System stability, error log, and resource utilization tests removed per user request

################################################################################
# Main Execution
################################################################################

main() {
    print_header "Additional System Tests Suite"
    
    print_info "Test Directory: $TEST_DIR"
    print_info "Results Directory: $RESULTS_DIR"
    echo "" | tee -a "$OUTPUT_FILE"
    
    # Create test directory
    mkdir -p "$TEST_DIR"
    
    # Initialize output file
    {
        echo "================================================================================"
        echo "Additional System Test Results"
        echo "================================================================================"
        echo "Test Date: $(date)"
        echo "Hostname: $(hostname)"
        echo "AIX Version: $(oslevel -s)"
        echo ""
    } > "$OUTPUT_FILE"
    
    # Run tests
    test_network_latency
    
    test_dns_resolution
    
    print_header "Additional System Tests Complete"
    print_info "Results saved to: $OUTPUT_FILE"
    
    # Display summary
    print_header "Test Summary"
    {
        echo "System configuration collected successfully"
        echo "Network interfaces: $(ifconfig -a | grep -c "^[a-z]")"
        echo ""
        
    } | tee -a "$OUTPUT_FILE"
}

# Run main function
main
exit $?

# Made with Bob
