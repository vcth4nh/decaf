package demo.text;

import java.util.Locale;

public final class TextTools {
    private TextTools() {}

    public static String normalize(String text) {
        return text.trim().replaceAll("\\s+", " ").toLowerCase(Locale.ROOT);
    }

    public static String[] words(String text) {
        String normalized = normalize(text);
        return normalized.isEmpty() ? new String[0] : normalized.split(" ");
    }
}
