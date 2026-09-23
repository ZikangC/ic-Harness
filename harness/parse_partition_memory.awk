function emit() {
    if (name == "")
        return
    if (complete)
        printf "%s\t%d\n", name, (found ? max_bits : 0)
    else
        printf "%s\tunobserved\n", name
}

/^HARNESS_PARTITION_BEGIN=/ {
    emit()
    name = substr($0, index($0, "=") + 1)
    max_bits = 0
    found = 0
    complete = 0
    next
}

$1 ~ /^[0-9][0-9]*$/ && $2 == "memory" && $3 == "bits" {
    value = $1 + 0
    if (!found || value > max_bits)
        max_bits = value
    found = 1
    next
}

/Number of memory bits/ {
    value = $NF + 0
    if (!found || value > max_bits)
        max_bits = value
    found = 1
    next
}

/MEMORY_STAT_COMPLETE=/ {
    complete = 1
}

END {
    emit()
}
