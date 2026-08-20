#!/usr/bin/perl
use strict;
use Time::HiRes qw(gettimeofday tv_interval);

# ==================== CONFIGURATION PARAMETERS ====================
# ADJUST THIS: Set the number of concurrent threads to test scalability.
# Try comparing 4 threads vs 16 threads!
my $num_threads = 16;        

# Total global problem size (Total chunks across all threads combined)
my $TOTAL_CHUNKS = 800;      

# Fixed allocation block size per cycle for 32-bit memory stability
my $chunk_size = 100_000;   
# ==================================================================

# Distribute the total work evenly among the requested threads
my $chunks_per_thread = int($TOTAL_CHUNKS / $num_threads);

print "=== AIX Multi-Threaded Memory Speed Benchmark ===\n";
print "Total Target Workload  : $TOTAL_CHUNKS global cycles\n";
print "Configured Thread Count: $num_threads parallel workers\n";
print "Workload Per Thread    : $chunks_per_thread cycles\n";
print "--------------------------------------------------\n";
print "Initializing. You have 3 seconds to switch to topas or vmstat...\n";

for (my $i = 3; $i > 0; $i--) { print "$i...\n"; sleep(1); }

print "\n🚀 Spawning $num_threads threads to flood memory channels...\n";

# Track exact memory footprint per element string
my $sample_string = "PowerServerBenchmarkData1234567890" . "100000";
my $bytes_per_element = length($sample_string); 

my $t0 = [gettimeofday];
my @pids = ();

for (my $t = 1; $t <= $num_threads; $t++) {
    my $pid = fork();
    
    if (!defined $pid) {
        die "Error: Failed to fork worker thread $t: $!\n";
    }
    elsif ($pid == 0) {
        # ------------ Child Thread: Dedicated Memory Burner ------------
        for (my $cycle = 1; $cycle <= $chunks_per_thread; $cycle++) {
            my @array;
            
            # 1. Write Access Allocation
            for (my $i = 0; $i < $chunk_size; $i++) {
                $array[$i] = "PowerServerBenchmarkData1234567890" . $i;
            }
            
            # 2. Read Access Sweeping
            my $dummy_count = 0;
            for (my $j = 0; $j < $chunk_size; $j += 2) {
                if ($array[$j]) { $dummy_count++; }
            }
            
            # Explicitly clear heap chunk
            @array = ();
        }
        # ---------------------------------------------------------------
        exit(0); # Exit child process safely
    }
    else {
        push(@pids, $pid); # Parent tracks process ID
    }
}

# Parent waits for all background memory burners to finish
print "All workers detached. Simulating parallel load on memory bus...\n";
foreach my $child_pid (@pids) {
    waitpid($child_pid, 0);
}

my $elapsed = tv_interval($t0);

# ==================== SCALED SPEED CALCULATIONS ====================
# Re-calculate total workload successfully executed by all threads
my $actual_completed_chunks = $chunks_per_thread * $num_threads;
my $total_writes = $actual_completed_chunks * $chunk_size;
my $total_reads  = $actual_completed_chunks * ($chunk_size / 2);

my $total_bytes_traffic = ($total_writes + $total_reads) * $bytes_per_element;
my $total_mb_traffic = $total_bytes_traffic / (1024 * 1024);
my $aggregate_speed_mb_s = $total_mb_traffic / $elapsed;
# ===================================================================

print "--------------------------------------------------\n";
print "🏁 Benchmark Report:\n";
printf "Total Execution Time        : %.4f seconds\n", $elapsed;
printf "Aggregate Data Processed    : %.2f MB\n", $total_mb_traffic;
print  "--------------------------------------------------\n";
printf "🚀 TOTAL MEMORY ACCESS SPEED : %.2f MB/s\n", $aggregate_speed_mb_s;
print  "--------------------------------------------------\n";