package demo.codec;

public final class Rot13 {
    private Rot13() {}

    public static String transform(String text) {
        StringBuilder result = new StringBuilder(text.length());
        for (char value : text.toCharArray()) {
            if (value >= 'a' && value <= 'z') {
                value = (char) ('a' + (value - 'a' + 13) % 26);
            } else if (value >= 'A' && value <= 'Z') {
                value = (char) ('A' + (value - 'A' + 13) % 26);
            }
            result.append(value);
        }
        return result.toString();
    }
}
