#!/bin/ksh
################################################################################
# CPU and Memory Benchmark Test Script for AIX/VIOS
# Description: Tests CPU and memory performance using mt_cpu_benchmark.pl
#              and mt_mem_benchmark.pl
# Usage: ./test_cpu_memory.sh [timestamp]
################################################################################

# Configuration
HOSTNAME=$(hostname -s)
TIMESTAMP=${1:-$(date +%Y%m%d_%H%M%S)}
SCRIPT_DIR=$(dirname $0)
TEST_DIR="${HOSTNAME}_cpu_memory"
mkdir -p "$TEST_DIR"
OUTPUT_FILE="${TEST_DIR}/${TIMESTAMP}_result.log"

# Benchmark scripts
MT_CPU_BENCHMARK="${SCRIPT_DIR}/mt_cpu_benchmark.pl"
MT_MEM_BENCHMARK="${SCRIPT_DIR}/mt_mem_benchmark.pl"

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

collect_cpu_info() {
    print_header "CPU and Memory Configuration"
    
    {
        echo "--- Processor Information ---"
        lsdev -Cc processor
        echo ""
        
        echo "--- CPU Details ---"
        lsattr -El proc0 2>/dev/null || echo "proc0 attributes not available"
        echo ""
        
        echo "--- System Configuration ---"
        lsattr -El sys0
        echo ""
        
        echo "--- Memory Information ---"
        lsattr -El sys0 -a realmem
        echo ""
        
        echo "--- Memory Statistics (svmon) ---"
        svmon -G
        echo ""
        
    } | tee -a "$OUTPUT_FILE"
}

run_cpu_benchmark() {
    print_header "CPU Performance Benchmark"
    
    if [[ ! -f "$MT_CPU_BENCHMARK" ]]; then
        print_error "mt_cpu_benchmark.pl not found at: $MT_CPU_BENCHMARK"
        return 1
    fi
    
    chmod +x "$MT_CPU_BENCHMARK"
    
    print_info "Running mt_cpu_benchmark.pl..."
    print_info "This will test CPU performance with multiple thread counts"
    echo "" | tee -a "$OUTPUT_FILE"
    
    {
        echo "=== mt_cpu_benchmark.pl Output ==="
        echo ""
        perl "$MT_CPU_BENCHMARK" 2>&1
        echo ""
        echo "=== mt_cpu_benchmark.pl Complete ==="
        echo ""
    } | tee -a "$OUTPUT_FILE"
    
    return 0
}

run_mem_benchmark() {
    print_header "Memory Performance Benchmark"
    
    if [[ ! -f "$MT_MEM_BENCHMARK" ]]; then
        print_error "mt_mem_benchmark.pl not found at: $MT_MEM_BENCHMARK"
        return 1
    fi
    
    chmod +x "$MT_MEM_BENCHMARK"
    
    print_info "Running mt_mem_benchmark.pl..."
    print_info "This will test memory bandwidth with multiple thread counts"
    echo "" | tee -a "$OUTPUT_FILE"
    
    {
        echo "=== mt_mem_benchmark.pl Output ==="
        echo ""
        perl "$MT_MEM_BENCHMARK" 2>&1
        echo ""
        echo "=== mt_mem_benchmark.pl Complete ==="
        echo ""
    } | tee -a "$OUTPUT_FILE"
    
    return 0
}

################################################################################
# Main Execution
################################################################################

main() {
    print_header "CPU and Memory Benchmark Test Suite"
    
    print_info "Test Directory: $TEST_DIR"
    print_info "Output File: $OUTPUT_FILE"
    print_info "Using mt_cpu_benchmark.pl and mt_mem_benchmark.pl"
    echo "" | tee -a "$OUTPUT_FILE"
    
    # Initialize output file
    {
        echo "================================================================================"
        echo "CPU and Memory Benchmark Results"
        echo "================================================================================"
        echo "Test Date: $(date)"
        echo "Hostname: $(hostname)"
        echo "AIX Version: $(oslevel -s)"
        echo ""
    } > "$OUTPUT_FILE"
    
    # Collect system information
    collect_cpu_info
    
    # Run CPU benchmark
    local cpu_result=0
    if run_cpu_benchmark; then
        print_info "CPU benchmark completed successfully"
    else
        print_error "CPU benchmark failed or not available"
        cpu_result=1
    fi
    
    echo "" | tee -a "$OUTPUT_FILE"
    sleep 2
    
    # Run Memory benchmark
    local mem_result=0
    if run_mem_benchmark; then
        print_info "Memory benchmark completed successfully"
    else
        print_error "Memory benchmark failed or not available"
        mem_result=1
    fi
    
    print_header "CPU and Memory Tests Complete"
    print_info "Results saved to: $OUTPUT_FILE"
    
    # Display summary
    print_header "Test Summary"
    {
        echo "Check the full log file for detailed results:"
        echo "$OUTPUT_FILE"
        echo ""
        
        if [[ $cpu_result -eq 0 ]]; then
            echo "CPU Benchmark Summary:"
            grep -i "elapsed\|operations\|score" "$OUTPUT_FILE" | tail -5
            echo ""
        fi
        
        if [[ $mem_result -eq 0 ]]; then
            echo "Memory Benchmark Summary:"
            grep -i "MB/s\|bandwidth\|throughput" "$OUTPUT_FILE" | tail -5
            echo ""
        fi
        
    } | tee -a "$OUTPUT_FILE"
    
    # Return error if both benchmarks failed
    if [[ $cpu_result -ne 0 && $mem_result -ne 0 ]]; then
        return 1
    fi
    
    return 0
}

# Run main function
main
exit $?

# Made with Bob
