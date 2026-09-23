# Extract numeric timing facts from OpenSTA's human-readable report_checks log.
#
# OpenSTA prints the measured value at the START of these lines, for example:
#   1689.858   data arrival time
#  -1684.275   slack (VIOLATED)
# The last field is prose ("time" or "(VIOLATED)"), so callers must never
# parse `$NF` as a number.  Only consume text bracketed by the harness markers;
# warnings elsewhere in the log are not timing evidence.
BEGIN {
  in_timing = 0
  done = 0
  arrival = ""
  slack = ""
  paths = 0
}
done { next }
/STA_TIMING_BEGIN/ {
  in_timing = 1
  next
}
/STA_TIMING_END/ {
  in_timing = 0
  done = 1
  next
}
# The server's mawk 1.3.3 does not reliably implement the POSIX character
# class used in an end-anchored expression.  Marker scoping already prevents
# unrelated log text from entering, so the portable literal match is enough.
in_timing && /data arrival time/ {
  for (i = 1; i <= NF; i++) {
    if ($i ~ /^-?[0-9]+(\.[0-9]+)?$/) {
      value = $i
      if (arrival == "" || value + 0 > arrival + 0) arrival = value
      break
    }
  }
  next
}
in_timing && /Startpoint:/ {
  # Each report path has one Startpoint but prints data arrival twice: once as
  # the arrival total and once in the required-minus-arrival slack arithmetic.
  paths++
  next
}
in_timing && /slack \(/ {
  for (i = 1; i <= NF; i++) {
    if ($i ~ /^-?[0-9]+(\.[0-9]+)?$/) {
      value = $i
      if (slack == "" || value + 0 < slack + 0) slack = value
      break
    }
  }
}
END {
  printf "arrival_ns=%s\n", arrival
  printf "slack_ns=%s\n", slack
  printf "path_count=%d\n", paths
}
