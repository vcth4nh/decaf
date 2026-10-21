// decaf compile checker (#99). Compiles each decompiled source file alone with
// javac (parse, attribution, and flow analysis; no class files) and reports its
// errors, so that decaf can find the files with decompiler errors.
//
// Usage: java -cp <dir> Checker <jobs file> <result file> <threads>
//
// Jobs file: tokens that end with a NUL byte. "job" <id> starts a job,
// "release" <n> gives a file with errors a second try with javac --release <n>
// (from the class-file version: old code may use removed JDK APIs, #108),
// "cp" <path> adds a classpath entry, and "src" <path> adds a source file
// (indexes 0, 1, 2, ... in this order).
//
// Result file: UTF-8 lines with TAB fields. The lines of a file are written
// and flushed when the file is done, so a kill at the timeout keeps them (#108):
//   <job> <index> E <P|A> <code> <line> <message>   one line for each error
//   <job> <index> X <throwable>                      javac threw for this file, or no thread finished it
//   <job> <index> T <milliseconds>                   the file is done
//
// A jar goes on the classpath as the root of its zip file system: javac then
// reads it as a folder and follows no Class-Path manifest of an input jar.
import com.sun.source.util.JavacTask;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileSystem;
import java.nio.file.FileSystems;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicInteger;
import javax.lang.model.SourceVersion;
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
        int release;  // 0: javac's own version
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
                run(compiler, job, threads, out);
            }
        }
    }

    private static void run(JavaCompiler compiler, Job job, int threads, Writer out) throws Exception {
        int n = job.sources.size();
        List<String> second = null;  // the options of the second try, or null for no second try
        if (job.release > 0 && supported(compiler, job.release) < supported(compiler, Integer.MAX_VALUE)) {
            second = new ArrayList<>(OPTIONS);
            second.add("--release");
            second.add(String.valueOf(supported(compiler, job.release)));
        }
        List<String> secondOptions = second;
        boolean[] done = new boolean[n];
        String[] stop = {"no result"};
        List<FileSystem> opened = new ArrayList<>();
        try {
            List<Path> classpath = new ArrayList<>();
            for (Path p : job.classpath) {
                Path entry = Files.isRegularFile(p) ? root(p, opened) : p;
                if (entry != null) {
                    classpath.add(entry);
                }
            }
            AtomicInteger next = new AtomicInteger();
            List<Thread> workers = new ArrayList<>();
            for (int w = 0; w < Math.min(threads, n); w++) {
                Thread t = new Thread(() -> {
                    // The second try gets its own file manager: --release sets the platform classes of it.
                    try (StandardJavaFileManager fm = open(compiler, classpath);
                         StandardJavaFileManager old = secondOptions == null ? null : open(compiler, classpath)) {
                        for (int i; (i = next.getAndIncrement()) < n; ) {
                            String r = check(compiler, fm, OPTIONS, job.id, i, job.sources.get(i));
                            if (old != null && failed(r)) {
                                String r2 = check(compiler, old, secondOptions, job.id, i, job.sources.get(i));
                                if (!failed(r2)) {
                                    r = r2;
                                }
                            }
                            write(out, r);
                            done[i] = true;
                        }
                    } catch (Throwable e) {
                        synchronized (stop) {
                            stop[0] = String.valueOf(e);
                        }
                    }
                });
                t.start();
                workers.add(t);
            }
            for (Thread t : workers) {
                t.join();
            }
        } catch (Throwable e) {
            stop[0] = String.valueOf(e);
        } finally {
            for (FileSystem fs : opened) {
                try {
                    fs.close();
                } catch (IOException e) {
                    // read-only: nothing to lose
                }
            }
        }
        for (int i = 0; i < n; i++) {
            if (!done[i]) {  // the error that stopped the thread is the cause for decaf (#108)
                write(out, job.id + "\t" + i + "\tX\t" + clean(stop[0]) + "\n");
            }
        }
    }

    private static StandardJavaFileManager open(JavaCompiler compiler, List<Path> classpath) throws IOException {
        StandardJavaFileManager fm = compiler.getStandardFileManager(null, Locale.ROOT, StandardCharsets.UTF_8);
        fm.setLocationFromPaths(StandardLocation.CLASS_PATH, classpath);
        fm.setLocationFromPaths(StandardLocation.SOURCE_PATH, List.of());
        fm.setLocationFromPaths(StandardLocation.ANNOTATION_PROCESSOR_PATH, List.of());
        return fm;
    }

    private static boolean failed(String result) {
        return result.contains("\tE\t") || result.contains("\tX\t");
    }

    /** The release nearest to the wanted one that this javac supports (an old javac cannot do 25, a new one maybe not 8). */
    private static int supported(JavaCompiler compiler, int wanted) {
        int lo = Integer.MAX_VALUE;
        int hi = 0;
        for (SourceVersion v : compiler.getSourceVersions()) {
            if (v.ordinal() >= 8) {
                lo = Math.min(lo, v.ordinal());
                hi = Math.max(hi, v.ordinal());
            }
        }
        return Math.max(lo, Math.min(hi, wanted));
    }

    /** The root of a jar's zip file system, or null when it cannot be opened (its classes count as absent). */
    private static Path root(Path jar, List<FileSystem> opened) {
        try {
            FileSystem fs = FileSystems.newFileSystem(jar, (ClassLoader) null);
            opened.add(fs);
            return fs.getPath("/");
        } catch (Throwable e) {
            return null;
        }
    }

    private static synchronized void write(Writer out, String text) throws IOException {
        out.write(text);
        out.flush();
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
            } else if (key.equals("release") && current != null) {
                current.release = Integer.parseInt(value);
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

    private static String check(JavaCompiler compiler, StandardJavaFileManager fm, List<String> options,
                                String job, int index, String source) {
        StringBuilder sb = new StringBuilder();
        String head = job + "\t" + index + "\t";
        DiagnosticCollector<JavaFileObject> dc = new DiagnosticCollector<>();
        long start = System.nanoTime();
        try {
            JavacTask task = (JavacTask) compiler.getTask(
                null, fm, dc, options, null, fm.getJavaFileObjects(source));
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
