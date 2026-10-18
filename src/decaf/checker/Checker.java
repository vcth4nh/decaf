// decaf compile checker (#99). Compiles each decompiled source file alone with
// javac (parse, attribution, and flow analysis; no class files) and reports its
// errors, so that decaf can find the files with decompiler errors.
//
// Usage: java -cp <dir> Checker <jobs file> <result file> <threads>
//
// Jobs file: tokens that end with a NUL byte. "job" <id> starts a job,
// "cp" <path> adds a classpath entry, and "src" <path> adds a source file
// (indexes 0, 1, 2, ... in this order).
//
// Result file: UTF-8 lines with TAB fields, written job by job:
//   <job> <index> E <P|A> <code> <line> <message>   one line for each error
//   <job> <index> X <throwable>                      javac threw for this file
//   <job> <index> T <milliseconds>                   the file is done
import com.sun.source.util.JavacTask;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicInteger;
import javax.tools.Diagnostic;
import javax.tools.DiagnosticCollector;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.StandardLocation;
import javax.tools.ToolProvider;

public final class Checker {
    private static final List<String> OPTIONS = List.of(
        "-proc:none", "-implicit:none", "-nowarn", "-Xlint:none",
        "-Xmaxerrs", "100000", "-XDshould-stop.ifError=FLOW");

    private static final class Job {
        final String id;
        final List<Path> classpath = new ArrayList<>();
        final List<String> sources = new ArrayList<>();

        Job(String id) {
            this.id = id;
        }
    }

    public static void main(String[] args) throws Exception {
        List<Job> jobs = readJobs(Path.of(args[0]));
        int threads = Math.max(1, Integer.parseInt(args[2]));
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        try (Writer out = Files.newBufferedWriter(Path.of(args[1]), StandardCharsets.UTF_8)) {
            for (Job job : jobs) {
                String[] results = new String[job.sources.size()];
                AtomicInteger next = new AtomicInteger();
                List<Thread> workers = new ArrayList<>();
                for (int w = 0; w < Math.min(threads, results.length); w++) {
                    Thread t = new Thread(() -> {
                        try (StandardJavaFileManager fm = compiler.getStandardFileManager(
                                null, Locale.ROOT, StandardCharsets.UTF_8)) {
                            fm.setLocationFromPaths(StandardLocation.CLASS_PATH, job.classpath);
                            fm.setLocationFromPaths(StandardLocation.SOURCE_PATH, List.of());
                            for (int i; (i = next.getAndIncrement()) < results.length; ) {
                                results[i] = check(compiler, fm, job.id, i, job.sources.get(i));
                            }
                        } catch (IOException e) {
                            // The files of this thread get no T line: decaf counts them unchecked.
                        }
                    });
                    t.start();
                    workers.add(t);
                }
                for (Thread t : workers) {
                    t.join();
                }
                for (String r : results) {
                    if (r != null) {
                        out.write(r);
                    }
                }
                out.flush();
            }
        }
    }

    private static List<Job> readJobs(Path file) throws IOException {
        String text = new String(Files.readAllBytes(file), StandardCharsets.UTF_8);
        String[] tokens = text.split("\0", -1);
        List<Job> jobs = new ArrayList<>();
        Job current = null;
        for (int i = 0; i + 1 < tokens.length; i += 2) {
            String key = tokens[i];
            String value = tokens[i + 1];
            if (key.equals("job")) {
                current = new Job(value);
                jobs.add(current);
            } else if (key.equals("cp") && current != null) {
                current.classpath.add(Path.of(value));
            } else if (key.equals("src") && current != null) {
                current.sources.add(value);
            } else {
                throw new IOException("bad jobs file token: " + key);
            }
        }
        return jobs;
    }

    private static String check(JavaCompiler compiler, StandardJavaFileManager fm,
                                String job, int index, String source) {
        StringBuilder sb = new StringBuilder();
        String head = job + "\t" + index + "\t";
        DiagnosticCollector<JavaFileObject> dc = new DiagnosticCollector<>();
        long start = System.nanoTime();
        try {
            JavacTask task = (JavacTask) compiler.getTask(
                null, fm, dc, OPTIONS, null, fm.getJavaFileObjects(source));
            task.parse();
            int parsed = dc.getDiagnostics().size();
            task.analyze();
            List<Diagnostic<? extends JavaFileObject>> all = dc.getDiagnostics();
            for (int i = 0; i < all.size(); i++) {
                Diagnostic<? extends JavaFileObject> d = all.get(i);
                if (d.getKind() != Diagnostic.Kind.ERROR) {
                    continue;
                }
                sb.append(head).append("E\t").append(i < parsed ? 'P' : 'A').append('\t')
                  .append(d.getCode()).append('\t').append(d.getLineNumber()).append('\t')
                  .append(clean(d.getMessage(Locale.ROOT))).append('\n');
            }
        } catch (Throwable t) {
            sb.append(head).append("X\t").append(clean(String.valueOf(t))).append('\n');
        }
        sb.append(head).append("T\t").append((System.nanoTime() - start) / 1_000_000).append('\n');
        return sb.toString();
    }

    private static String clean(String s) {
        return s.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ');
    }
}
