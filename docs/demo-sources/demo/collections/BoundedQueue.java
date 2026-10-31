package demo.collections;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

public final class BoundedQueue<T> {
    private final int capacity;
    private final Deque<T> values = new ArrayDeque<>();

    public BoundedQueue(int capacity) {
        if (capacity <= 0) {
            throw new IllegalArgumentException("Capacity must be positive");
        }
        this.capacity = capacity;
    }

    public void add(T value) {
        if (values.size() == capacity) {
            values.removeFirst();
        }
        values.addLast(value);
    }

    public List<T> snapshot() {
        return new ArrayList<>(values);
    }
}
