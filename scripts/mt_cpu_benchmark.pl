#!/usr/bin/perl
use strict;
use Time::HiRes qw(gettimeofday tv_interval);

# ==================== CONFIGURATION PARAMETERS ====================
# The TOTAL problem size (Total iterations to be solved globally).
# This remains constant regardless of how many threads you throw at it.
my $TOTAL_WORKLOAD = 300_000_000; 

# Adjust this to see how splitting the labor affects performance.
# Try running it with 2, 4, then 8 threads to compare!
my $num_threads = 8;        
# ==================================================================

# Calculate the precise slice of labor assigned to each individual thread
my $loops_per_thread = int($TOTAL_WORKLOAD / $num_threads);

print "=== AIX Data-Parallel Benchmark (Divide & Conquer) ===\n";
print "Total Problem Size : $TOTAL_WORKLOAD loops\n";
print "Number of Threads  : $num_threads\n";
print "Work Per Thread    : $loops_per_thread loops\n";
print "--------------------------------------------------\n";
print "Initializing. 3 seconds to switch windows to 'topas' or 'vmstat'...\n";

for (my $i = 3; $i > 0; $i--) {
    print "$i...\n";
    sleep(1);
}

print "\n🚀 Launching parallel workers to split the labor...\n";
my $t0 = [gettimeofday];
my @pids = ();

for (my $t = 1; $t <= $num_threads; $t++) {
    my $pid = fork();
    
    if (!defined $pid) {
        die "Error: Failed to fork thread $t: $!\n";
    }
    elsif ($pid == 0) {
        # ------------ Thread Worker: Processes its assigned chunk ------------
        my $calc = 0;
        my @local_mem;
        
        for (my $i = 0; $i < $loops_per_thread; $i++) {
            # CPU Math
            $calc = sin($i) * cos($i);
            
            # Memory allocation (safe throttled mapping)
            if ($i % 500 == 0) {
                $local_mem[$i / 500] = "ParallelChunk_Thread_$t" . $i;
            }
        }
        # ---------------------------------------------------------------------
        exit(0); 
    }
    else {
        push(@pids, $pid);
    }
}

print "All workers are processing their pieces of the puzzle. Waiting...\n";
foreach my $child_pid (@pids) {
    waitpid($child_pid, 0);
}

my $elapsed = tv_interval($t0);
print "--------------------------------------------------\n";
print "🏁 Problem Solved Successfully!\n";
printf "Total Execution Time with $num_threads threads: %.4f seconds\n", $elapsed;
print "--------------------------------------------------\n";