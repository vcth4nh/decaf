package demo.math;

public final class Statistics {
    private Statistics() {}

    public static double mean(int[] values) {
        if (values.length == 0) {
            throw new IllegalArgumentException("No values");
        }
        long total = 0;
        for (int value : values) {
            total += value;
        }
        return (double) total / values.length;
    }

    public static int maximum(int[] values) {
        if (values.length == 0) {
            throw new IllegalArgumentException("No values");
        }
        int result = values[0];
        for (int value : values) {
            result = Math.max(result, value);
        }
        return result;
    }
}
