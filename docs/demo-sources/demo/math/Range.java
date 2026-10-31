package demo.math;

public final class Range {
    private final int lower;
    private final int upper;

    public Range(int lower, int upper) {
        if (lower > upper) {
            throw new IllegalArgumentException("Reversed bounds");
        }
        this.lower = lower;
        this.upper = upper;
    }

    public boolean contains(int value) {
        return value >= lower && value <= upper;
    }

    public int clamp(int value) {
        return Math.max(lower, Math.min(upper, value));
    }
}
