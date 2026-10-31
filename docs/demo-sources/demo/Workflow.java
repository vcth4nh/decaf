package demo;

import demo.codec.HexCodec;
import demo.codec.Rot13;
import demo.collections.BoundedQueue;
import demo.collections.Pair;
import demo.math.Range;
import demo.math.Statistics;
import demo.text.TextTools;
import demo.text.WordCounter;
import java.nio.charset.StandardCharsets;

public class Workflow {
    public static void main(String[] args) {
        String text = TextTools.normalize(" Hello   decaf hello ");
        BoundedQueue<String> recent = new BoundedQueue<>(2);
        for (String word : TextTools.words(text)) {
            recent.add(word);
        }
        String encoded = HexCodec.encode(text.getBytes(StandardCharsets.UTF_8));
        System.out.println(new String(HexCodec.decode(encoded), StandardCharsets.UTF_8));
        System.out.println(WordCounter.count(text));
        System.out.println(new Pair<>(recent.snapshot(), Rot13.transform(text)));
        System.out.println(Statistics.mean(new int[] {2, 4, 6}));
        System.out.println(new Range(0, 10).clamp(new Calculator().increment(10)));
    }
}
