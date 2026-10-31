package demo.text;

import java.util.Map;
import java.util.TreeMap;

public final class WordCounter {
    private WordCounter() {}

    public static Map<String, Integer> count(String text) {
        Map<String, Integer> counts = new TreeMap<>();
        for (String word : TextTools.words(text)) {
            counts.put(word, counts.getOrDefault(word, 0) + 1);
        }
        return counts;
    }
}
