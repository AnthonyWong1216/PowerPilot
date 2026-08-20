#!/bin/ksh
################################################################################
# I/O Performance Test Script for AIX/VIOS
# Description: Tests disk I/O performance using dd and optional fio
# Usage: ./test_io_performance.sh [test_directory] [timestamp]
################################################################################

# Configuration
TEST_DIR=${1:-"/tmp/aix_test_data"}
TIMESTAMP=${2:-$(date +%Y%m%d_%H%M%S)}
HOSTNAME=$(hostname -s)
RESULTS_DIR="${HOSTNAME}_io_performance"
mkdir -p "$RESULTS_DIR"
OUTPUT_FILE="${RESULTS_DIR}/${TIMESTAMP}_result.log"

# Test parameters
TEST_FILE="${TEST_DIR}/io_test_file"
FILE_SIZE_MB=1024
BLOCK_SIZES="4k 64k 1m"
DD_COUNT=1024
FIO_BIN="./fio-2.0/fio"

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

cleanup_test_files() {
    print_info "Cleaning up test files..."
    rm -f ${TEST_FILE}* 2>/dev/null
}

convert_fio_output() {
    # Convert fio KB/s output to MB/s
    while IFS= read -r line; do
        if echo "$line" | grep -q "bw="; then
            # Extract bandwidth value and unit
            local bw_value=$(echo "$line" | sed 's/.*bw=\([0-9]*\)\([KMG]*B\/s\).*/\1/')
            local bw_unit=$(echo "$line" | sed 's/.*bw=[0-9]*\([KMG]*B\/s\).*/\1/')
            
            # Convert to MB/s
            local bw_mbs=0
            if echo "$bw_unit" | grep -q "KB"; then
                bw_mbs=$(echo "scale=2; $bw_value / 1024" | bc)
            elif echo "$bw_unit" | grep -q "MB"; then
                bw_mbs=$bw_value
            elif echo "$bw_unit" | grep -q "GB"; then
                bw_mbs=$(echo "scale=2; $bw_value * 1024" | bc)
            fi
            
            # Replace KB/s with MB/s in output
            echo "$line" | sed "s/bw=[0-9]*[KMG]*B\/s/bw=${bw_mbs}MB\/s/"
        else
            echo "$line"
        fi
    done
}

test_with_fio() {
    print_header "FIO Performance Tests"
    
    if [[ ! -f "$FIO_BIN" ]]; then
        print_info "FIO not found at $FIO_BIN, skipping FIO tests"
        return
    fi
    
    chmod +x "$FIO_BIN"
    
    # Sequential read test
    print_info "FIO Sequential Read Test (1MB blocks, 1GB size)"
    $FIO_BIN --name=seqread --rw=read --bs=1m --size=1g --numjobs=1 \
        --filename=${TEST_FILE}_fio --direct=1 --ioengine=sync 2>&1 | \
        grep -E "read :" | convert_fio_output | tee -a "$OUTPUT_FILE"
    echo "" | tee -a "$OUTPUT_FILE"
    
    # Sequential write test
    print_info "FIO Sequential Write Test (1MB blocks, 1GB size)"
    $FIO_BIN --name=seqwrite --rw=write --bs=1m --size=1g --numjobs=1 \
        --filename=${TEST_FILE}_fio --direct=1 --ioengine=sync 2>&1 | \
        grep -E "write:" | convert_fio_output | tee -a "$OUTPUT_FILE"
    echo "" | tee -a "$OUTPUT_FILE"
    
    # Random read test
    print_info "FIO Random Read Test (4K blocks, 30s runtime)"
    $FIO_BIN --name=randread --rw=randread --bs=4k --size=1g --numjobs=1 \
        --filename=${TEST_FILE}_fio --direct=1 --ioengine=sync --runtime=30 --time_based 2>&1 | \
        grep -E "read :" | convert_fio_output | tee -a "$OUTPUT_FILE"
    echo "" | tee -a "$OUTPUT_FILE"
    
    rm -f ${TEST_FILE}_fio
}

collect_disk_info() {
    print_header "Disk Configuration Information"
    
    {
        echo "--- Physical Volumes ---"
        lspv
        echo ""
        
        echo "--- Volume Groups ---"
        lsvg
        echo ""
        
        lsvg -o | while read vg; do
            echo "--- Volume Group: $vg ---"
            lsvg $vg
            echo ""
        done
        
        echo "--- Filesystem Information ---"
        df -g
        echo ""
        
    } | tee -a "$OUTPUT_FILE"
}

################################################################################
# Main Execution
################################################################################

main() {
    print_header "I/O Performance Test Suite"
    
    print_info "Test Directory: $TEST_DIR"
    print_info "Results Directory: $RESULTS_DIR"
    print_info "Test File Size: ${FILE_SIZE_MB} MB"
    echo "" | tee -a "$OUTPUT_FILE"
    
    # Create test directory
    mkdir -p "$TEST_DIR"
    
    # Check if test directory is writable
    if [[ ! -w "$TEST_DIR" ]]; then
        print_error "Test directory is not writable: $TEST_DIR"
        exit 1
    fi
    
    # Initialize output file
    {
        echo "================================================================================"
        echo "I/O Performance Test Results"
        echo "================================================================================"
        echo "Test Date: $(date)"
        echo "Hostname: $(hostname)"
        echo "AIX Version: $(oslevel -s)"
        echo "Test Directory: $TEST_DIR"
        echo ""
    } > "$OUTPUT_FILE"
    
    # Collect disk information
    collect_disk_info
    
    # FIO tests (primary I/O testing tool)
    test_with_fio
    
    # Cleanup
    cleanup_test_files
    
    print_header "I/O Performance Tests Complete"
    print_info "Results saved to: $OUTPUT_FILE"
    
    # Display summary
    print_header "Test Summary"
    {
        echo "FIO Test Results (all speeds in MB/s):"
        grep "bw=" "$OUTPUT_FILE" | tail -3
        echo ""
        
    } | tee -a "$OUTPUT_FILE"
}

# Run main function
main
exit $?

# Made with Bob
