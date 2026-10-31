package demo.codec;

public final class HexCodec {
    private static final char[] DIGITS = "0123456789abcdef".toCharArray();

    private HexCodec() {}

    public static String encode(byte[] data) {
        char[] result = new char[data.length * 2];
        for (int i = 0; i < data.length; i++) {
            int value = data[i] & 255;
            result[i * 2] = DIGITS[value >>> 4];
            result[i * 2 + 1] = DIGITS[value & 15];
        }
        return new String(result);
    }

    public static byte[] decode(String text) {
        if (text.length() % 2 != 0) {
            throw new IllegalArgumentException("Odd hex length");
        }
        byte[] result = new byte[text.length() / 2];
        for (int i = 0; i < result.length; i++) {
            int high = Character.digit(text.charAt(i * 2), 16);
            int low = Character.digit(text.charAt(i * 2 + 1), 16);
            if (high < 0 || low < 0) {
                throw new IllegalArgumentException("Invalid hex digit");
            }
            result[i] = (byte) ((high << 4) | low);
        }
        return result;
    }
}
