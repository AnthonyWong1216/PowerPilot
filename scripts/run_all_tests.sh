#!/bin/ksh
################################################################################
# Master Test Orchestrator for AIX/VIOS Testing
# Description: Runs all test scripts in sequence and collects results
# Usage: ./run_all_tests.sh [test_directory]
################################################################################

# Configuration
SCRIPT_DIR=$(dirname $0)
TEST_DIR=${1:-"/tmp/aix_test_data"}
HOSTNAME=$(hostname -s)
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_BASE_DIR="."

# Colors for output (if terminal supports)
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m' # No Color
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

################################################################################
# Functions
################################################################################

print_header() {
    echo ""
    echo "================================================================================"
    echo "$1"
    echo "================================================================================"
    echo ""
}

print_info() {
    echo "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo "${RED}[ERROR]${NC} $1"
}

check_prerequisites() {
    print_header "Checking Prerequisites"
    
    local missing_tools=0
    
    # Check for required commands
    for cmd in perl bc; do
        if ! command -v $cmd >/dev/null 2>&1; then
            print_error "Required command not found: $cmd"
            missing_tools=1
        else
            print_success "Found: $cmd"
        fi
    done
    
    # Check for optional tools
    if command -v iperf >/dev/null 2>&1; then
        print_success "Found: iperf"
    else
        print_warning "Optional tool not found: iperf (network tests will be skipped)"
    fi
    
    # Check for fio in local directory
    if [[ -f "./fio-2.0/fio" ]]; then
        print_success "Found: ./fio-2.0/fio"
    else
        print_warning "Optional tool not found: ./fio-2.0/fio (I/O tests will be skipped)"
    fi
    
    # Check for benchmark scripts
    if [[ -f "./mt_cpu_benchmark.pl" ]]; then
        print_success "Found: ./mt_cpu_benchmark.pl"
    else
        print_warning "Optional tool not found: ./mt_cpu_benchmark.pl"
    fi
    
    if [[ -f "./mt_mem_benchmark.pl" ]]; then
        print_success "Found: ./mt_mem_benchmark.pl"
    else
        print_warning "Optional tool not found: ./mt_mem_benchmark.pl"
    fi
    
    if [[ $missing_tools -eq 1 ]]; then
        print_error "Missing required tools. Please install them before continuing."
        return 1
    fi
    
    return 0
}

collect_system_info() {
    print_header "Collecting System Information"
    
    local test_dir="${RESULTS_BASE_DIR}/${HOSTNAME}_system_info"
    mkdir -p "$test_dir"
    local output_file="${test_dir}/${TIMESTAMP}_result.log"
    
    {
        echo "=== System Information Collected at $(date) ==="
        echo ""
        
        echo "--- Hostname ---"
        hostname
        echo ""
        
        echo "--- AIX Version ---"
        oslevel -s
        echo ""
        
        echo "--- Hardware Info ---"
        prtconf | head -20
        echo ""
        
        echo "--- CPU Info ---"
        lsdev -Cc processor
        echo ""
        
        echo "--- Memory Info ---"
        lsattr -El sys0 -a realmem
        svmon -G
        echo ""
        
        echo "--- Disk Info ---"
        lspv
        echo ""
        
        echo "--- Volume Groups ---"
        lsvg
        lsvg -o | while read vg; do
            echo "VG: $vg"
            lsvg $vg
            echo ""
        done
        
        echo "--- Network Interfaces ---"
        netstat -in
        echo ""
        
        echo "--- Network Configuration ---"
        ifconfig -a
        echo ""
        
        echo "--- Installed Filesets (sample) ---"
        lslpp -l | head -20
        echo ""
        
    } > "$output_file"
    
    print_success "System information saved to: $output_file"
}

run_test() {
    local test_name=$1
    local test_script=$2
    shift 2
    local test_args="$@"
    
    print_header "Running: $test_name"
    
    if [[ ! -f "$test_script" ]]; then
        print_error "Test script not found: $test_script"
        return 1
    fi
    
    if [[ ! -x "$test_script" ]]; then
        print_warning "Making script executable: $test_script"
        chmod +x "$test_script"
    fi
    
    local start_time=$(date +%s)
    print_info "Start time: $(date)"
    
    # Run the test
    if $test_script $test_args; then
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        print_success "$test_name completed in ${duration}s"
        return 0
    else
        local end_time=$(date +%s)
        local duration=$((end_time - start_time))
        print_error "$test_name failed after ${duration}s"
        return 1
    fi
}

generate_summary() {
    print_header "Generating Test Summary"
    
    local summary_dir="${RESULTS_BASE_DIR}/${HOSTNAME}_test_summary"
    mkdir -p "$summary_dir"
    local summary_file="${summary_dir}/${TIMESTAMP}_result.log"
    
    {
        echo "================================================================================"
        echo "AIX/VIOS Test Suite - Summary Report"
        echo "================================================================================"
        echo "Test Date: $(date)"
        echo "Hostname: $(hostname)"
        echo "AIX Version: $(oslevel -s)"
        echo "Results Base Directory: $RESULTS_BASE_DIR"
        echo ""
        echo "================================================================================"
        echo "Test Results"
        echo "================================================================================"
        echo ""
        
        # List all test directories
        for test_dir in ${RESULTS_BASE_DIR}/${HOSTNAME}_*; do
            if [[ -d "$test_dir" ]]; then
                echo "--- $(basename $test_dir) ---"
                ls -lh ${test_dir}/${TIMESTAMP}_result.log 2>/dev/null | awk '{print "File: " $9 " Size: " $5}'
                echo ""
            fi
        done
        
        echo "================================================================================"
        echo "Quick Results Summary"
        echo "================================================================================"
        echo ""
        
        # Extract key metrics from logs
        if [[ -f "${RESULTS_BASE_DIR}/${HOSTNAME}_network_throughput/${TIMESTAMP}_result.log" ]]; then
            echo "Network Throughput:"
            grep -i "bits/sec\|Mbits/sec\|Gbits/sec" "${RESULTS_BASE_DIR}/${HOSTNAME}_network_throughput/${TIMESTAMP}_result.log" | tail -5
            echo ""
        fi
        
        if [[ -f "${RESULTS_BASE_DIR}/${HOSTNAME}_io_performance/${TIMESTAMP}_result.log" ]]; then
            echo "I/O Performance:"
            grep -i "MB/s\|IOPS\|latency" "${RESULTS_BASE_DIR}/${HOSTNAME}_io_performance/${TIMESTAMP}_result.log" | tail -10
            echo ""
        fi
        
        if [[ -f "${RESULTS_BASE_DIR}/${HOSTNAME}_cpu_memory/${TIMESTAMP}_result.log" ]]; then
            echo "CPU/Memory:"
            grep -i "score\|bandwidth\|operations/sec\|MB/s" "${RESULTS_BASE_DIR}/${HOSTNAME}_cpu_memory/${TIMESTAMP}_result.log" | tail -10
            echo ""
        fi
        
        echo "================================================================================"
        echo "End of Summary"
        echo "================================================================================"
        
    } > "$summary_file"
    
    print_success "Summary report saved to: $summary_file"
    
    # Display summary to console
    cat "$summary_file"
}

################################################################################
# Main Execution
################################################################################

main() {
    print_header "AIX/VIOS Test Suite - Master Orchestrator"
    
    print_info "Script Directory: $SCRIPT_DIR"
    print_info "Test Data Directory: $TEST_DIR"
    print_info "Results Base Directory: $RESULTS_BASE_DIR"
    print_info "Timestamp: $TIMESTAMP"
    echo ""
    
    # Check prerequisites
    if ! check_prerequisites; then
        exit 1
    fi
    
    # Collect system information
    collect_system_info
    
    # Create test data directory
    mkdir -p "$TEST_DIR"
    
    # Run tests
    local failed_tests=0
    
    # CPU and Memory Test (no dependencies)
    if ! run_test "CPU and Memory Benchmark" \
        "${SCRIPT_DIR}/test_cpu_memory.sh" \
        "$TIMESTAMP"; then
        failed_tests=$((failed_tests + 1))
    fi
    
    # I/O Performance Test
    if ! run_test "I/O Performance Test" \
        "${SCRIPT_DIR}/test_io_performance.sh" \
        "$TEST_DIR" "$TIMESTAMP"; then
        failed_tests=$((failed_tests + 1))
    fi
    
    # Additional System Tests
    if ! run_test "Additional System Tests" \
        "${SCRIPT_DIR}/test_system_additional.sh" \
        "$TEST_DIR" "$TIMESTAMP"; then
        failed_tests=$((failed_tests + 1))
    fi
    
    # Network test requires manual setup
    print_header "Network Throughput Test"
    print_warning "Network test requires two servers and manual setup"
    print_info "To run network test:"
    print_info "  Server 1: ${SCRIPT_DIR}/test_network_throughput.sh server $TIMESTAMP"
    print_info "  Server 2: ${SCRIPT_DIR}/test_network_throughput.sh client <server1_ip> $TIMESTAMP"
    echo ""
    
    # Generate summary
    generate_summary
    
    # Final status
    print_header "Test Suite Complete"
    
    if [[ $failed_tests -eq 0 ]]; then
        print_success "All tests completed successfully!"
    else
        print_warning "$failed_tests test(s) failed or were skipped"
    fi
    
    print_info "Results available in: ${RESULTS_BASE_DIR}/${HOSTNAME}_*/"
    
    return $failed_tests
}

# Run main function
main "$@"
exit $?

# Made with Bob
