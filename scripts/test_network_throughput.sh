#!/bin/ksh
################################################################################
# Network Throughput Test Script for AIX/VIOS
# Description: Simple network throughput test using iperf (TCP and UDP only)
# Usage: 
#   Server mode: ./test_network_throughput.sh server [iperf_port] [timestamp]
#   Client mode: ./test_network_throughput.sh client <server_ip> [iperf_port] [duration] [udp_bw] [timestamp]
################################################################################

# Configuration
MODE=${1:-""}
SERVER_IP=${2:-""}
HOSTNAME=$(hostname -s)

# Configurable parameters with defaults (overridden by positional args)
case "$MODE" in
    server)
        IPERF_PORT=${2:-5201}
        TIMESTAMP=${3:-$(date +%Y%m%d_%H%M%S)}
        ;;
    client)
        # client <server_ip> [port] [duration] [udp_bw] [timestamp]
        IPERF_PORT=${3:-5201}
        TEST_DURATION=${4:-10}
        UDP_BANDWIDTH=${5:-"1G"}
        TIMESTAMP=${6:-$(date +%Y%m%d_%H%M%S)}
        ;;
    *)
        IPERF_PORT=5201
        TEST_DURATION=10
        UDP_BANDWIDTH="1G"
        TIMESTAMP=$(date +%Y%m%d_%H%M%S)
        ;;
esac

TEST_DIR="/tmp/${HOSTNAME}_network_throughput"
mkdir -p "$TEST_DIR"
OUTPUT_FILE="${TEST_DIR}/${TIMESTAMP}_result.log"

# Check for iperf
if command -v iperf >/dev/null 2>&1; then
    IPERF_CMD="iperf"
else
    echo "ERROR: iperf not found. Please install iperf."
    exit 1
fi

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

usage() {
    echo "Usage:"
    echo "  Server mode: $0 server [iperf_port] [timestamp]"
    echo "  Client mode: $0 client <server_ip> [iperf_port] [duration] [udp_bw] [timestamp]"
    echo ""
    echo "Examples:"
    echo "  On Server: ./test_network_throughput.sh server 5201"
    echo "  On Client: ./test_network_throughput.sh client 192.168.1.100 5201 10 1G"
    exit 1
}

run_server_mode() {
    print_header "Starting Network Throughput Test - SERVER MODE"
    
    print_info "Server listening on port $IPERF_PORT"
    print_info "Using: $IPERF_CMD"
    print_info "Press Ctrl+C to stop the server"
    echo ""
    
    $IPERF_CMD -s -p $IPERF_PORT
}

test_tcp_throughput() {
    print_info "Testing TCP throughput..."
    $IPERF_CMD -c $SERVER_IP -p $IPERF_PORT -t $TEST_DURATION 2>&1 | tee -a "$OUTPUT_FILE"
    echo "" | tee -a "$OUTPUT_FILE"
}

test_udp_throughput() {
    print_info "Testing UDP throughput with bandwidth: $UDP_BANDWIDTH"
    $IPERF_CMD -c $SERVER_IP -p $IPERF_PORT -t $TEST_DURATION -u -b $UDP_BANDWIDTH 2>&1 | tee -a "$OUTPUT_FILE"
    echo "" | tee -a "$OUTPUT_FILE"
}

run_client_mode() {
    print_header "Starting Network Throughput Test - CLIENT MODE"
    
    print_info "Target Server: $SERVER_IP"
    print_info "Port: $IPERF_PORT"
    print_info "Using: $IPERF_CMD"
    print_info "Test Duration: ${TEST_DURATION}s per test"
    echo "" | tee -a "$OUTPUT_FILE"
    
    # Test connectivity
    print_info "Testing connectivity to $SERVER_IP..."
    if ! ping -c 5 $SERVER_IP >/dev/null 2>&1; then
        print_error "Cannot ping $SERVER_IP. Please check network connectivity."
        exit 1
    fi
    print_info "Connectivity OK"
    echo "" | tee -a "$OUTPUT_FILE"
    
    # TCP Throughput Test
    print_header "TCP Throughput Test"
    test_tcp_throughput
    sleep 2
    
    # UDP Throughput Test
    print_header "UDP Throughput Test"
    test_udp_throughput
    
    print_header "Network Throughput Tests Complete"
    print_info "Results saved to: $OUTPUT_FILE"
}

################################################################################
# Main Execution
################################################################################

main() {
    # Initialize output file
    {
        echo "================================================================================"
        echo "Network Throughput Test Results"
        echo "================================================================================"
        echo "Test Date: $(date)"
        echo "Hostname: $(hostname)"
        echo "AIX Version: $(oslevel -s)"
        echo "iperf Command: $IPERF_CMD"
        echo ""
    } > "$OUTPUT_FILE"
    
    case "$MODE" in
        server)
            run_server_mode
            ;;
        client)
            if [[ -z "$SERVER_IP" ]]; then
                print_error "Server IP address required for client mode"
                usage
            fi
            run_client_mode
            ;;
        *)
            print_error "Invalid mode: $MODE"
            usage
            ;;
    esac
}

# Validate arguments
if [[ -z "$MODE" ]]; then
    usage
fi

if [[ "$MODE" == "client" && -z "$SERVER_IP" ]]; then
    usage
fi

# Run main function
main
exit $?

# Made with Bob
