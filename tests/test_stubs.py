from pathlib import Path

import pytest

from decaf import stubs
from decaf.stubs import Stub

VF_PLEASE = (
    "Please report this to the Vineflower issue tracker, at https://github.com/Vineflower/vineflower/issues"
    " with a copy of the class file (if you have the rights to distribute it!)"
)

# Real Vineflower 1.12.0 output: spring-boot-3.5.12 StructuredLoggingJsonProperties (trimmed).
VF_METHOD = (
    "   Collection<StructuredLoggingJsonMembersCustomizer<Object>> customizers(Instantiator<?> instantiator) {\n"
    "      // $VF: Couldn't be decompiled\n"
    f"      // {VF_PLEASE}\n"
    '      // java.lang.NullPointerException: Cannot read field "value" because "returnType" is null\n'
    "      //   at org.jetbrains.java.decompiler.modules.decompiler.exps.NewExprent.getInferredExprType(NewExprent.java:226)\n"
    "      //\n"
    "      // Bytecode:\n"
    "      // 00: aload 0\n"
    "      // 1e: areturn\n"
    "   }\n"
)

# Real: bcprov-jdk18on-1.78.1 ReedSolomon under a 1 GiB heap (trimmed).
VF_OOM = (
    "   static {\n"
    "      // $VF: Couldn't be decompiled\n"
    f"      // {VF_PLEASE}\n"
    "      // java.lang.OutOfMemoryError: Java heap space\n"
    "      //   at org.jetbrains.java.decompiler.util.collections.FastSparseSetFactory$FastSparseSet.getCopy(FastSparseSetFactory.java:96)\n"
    "   }\n"
)

# Real: spring-web-6.2.17 ServerWebExchangeExtensionsKt.kt has four stubbed functions (trimmed).
_KT_STUB = (
    "   // $VF: Couldn't be decompiled\n"
    f"   // {VF_PLEASE}\n"
    "   // java.lang.IllegalStateException: Anonymous class does not have Class Kotlin metadata\n"
    "   //   at org.vineflower.kotlin.KotlinWriter.writeClassDefinition(KotlinWriter.java:742)\n"
)
VF_KOTLIN = "package org.springframework.web.server\n\n" + "".join(
    f"public suspend fun ServerWebExchange.{name}(): Any {{\n{_KT_STUB}}}\n\n"
    for name in ("awaitFormData", "awaitMultipartData", "awaitPrincipal", "awaitSession")
)

# Built from Vineflower 1.12.0 Fernflower.getClassContent: a block comment, no // prefix.
VF_CLASS = (
    "/*\n"
    "$VF: Unable to decompile class\n"
    f"{VF_PLEASE}\n"
    "java.lang.IllegalStateException: boom\n"
    "  at org.jetbrains.java.decompiler.main.ClassWriter.classToJava(ClassWriter.java:1)\n"
    "*/"
)
VF_CLASS_EMPTY = "$ VF: Unable to decompile class com.x.A"  # the class gave no output at all

# Built from Vineflower 1.12.0 ClassWriter.invokeProcessors and its lambda writer.
VF_INLINE = (
    "class A {\n"
    "   Object o = /* $VF: Couldn't be decompiled. Class com.x.A$1 wasn't processed yet! */"
    f"// {VF_PLEASE}\n"
    ";\n"
    "}\n"
)
VF_LAMBDA = "class A {\n   Runnable r = () -> // $VF: Couldn't be decompiled\n   ;\n}\n"

# Built from Fernflower 253 ClassWriter.methodToJava and LimitContainer: the default text, or a limit message.
FF_METHOD = "class A {\n   public void m() {\n      // $FF: Couldn't be decompiled\n   }\n}\n"
FF_LIMIT = (
    "class A {\n   public void m() {\n"
    "      // $FF: Limits for direct nodes are exceeded. Current value: 20001, limit: 20000\n   }\n}\n"
)

# Real CFR 0.152 output: jenkins-core-2.568.1 hudson/Util.java (trimmed).
CFR_METHOD = (
    "    public static String loadFile(@NonNull File logfile, @NonNull Charset charset) throws IOException {\n"
    "        /*\n"
    "         * This method has failed to decompile.  When submitting a bug report, please provide this stack trace,"
    " and (if you hold appropriate legal rights) the relevant class file.\n"
    "         * \n"
    "         * org.benf.cfr.reader.util.ConfusedCFRException: Started 2 blocks at once\n"
    "         *     at org.benf.cfr.reader.bytecode.analysis.opgraph.Op04StructuredStatement.getStartingBlocks(Op04StructuredStatement.java:412)\n"
    "         */\n"
    '        throw new IllegalStateException("Decompilation failed");\n'
    "    }\n"
)

# Real Procyon 0.6.0 output: jenkins-core-2.568.1 ClassFilterImpl (trimmed bytecode listing).
PROCYON_METHOD = (
    "    private boolean isLocationWhitelisted(final String _loc) {\n"
    "        // \n"
    "        // This method could not be decompiled.\n"
    "        // \n"
    "        // Original Bytecode:\n"
    "        // \n"
    "        //     1: getfield        jenkins/security/ClassFilterImpl.codeSourceCache:Ljava/util/Map;\n"
    "        //     4: aload_1         /* _loc */\n"
    "        //    21: ireturn        \n"
    "        //    MethodParameters:\n"
    "        //  Name  Flags  \n"
    "        //  ----  -----\n"
    "        //  _loc  \n"
    "        // \n"
    "        // The error that occurred was:\n"
    "        // \n"
    "        // java.lang.IllegalStateException: Could not infer any expression.\n"
    "        //     at com.strobel.decompiler.ast.TypeAnalysis.runInference(TypeAnalysis.java:382)\n"
    "        // \n"
    '        throw new IllegalStateException("An error occurred while decompiling this method.");\n'
    "    }\n"
)

# Real JD-CLI 1.2.0 output: jenkins-core-2.568.1 ModelObjectWithContextMenu.java (trimmed).
JD_METHOD = (
    "    public ContextMenu add(Action action) {\n"
    "      // Byte code:\n"
    "      //   0: new jenkins/model/ModelObjectWithContextMenu$MenuItem\n"
    "      //   3: dup\n"
    "      //   4: invokespecial <init> : ()V\n"
    "    }\n"
)


def write(tree: Path, rel: str, text: str) -> None:
    p = tree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.mark.parametrize(
    "engine, name, text, methods, errors",
    [
        ("vineflower", "A.java", VF_METHOD, 1, ("java.lang.NullPointerException",)),
        ("vineflower", "A.kt", VF_KOTLIN, 4, ("java.lang.IllegalStateException",)),
        ("vineflower", "A.java", VF_OOM, 1, ("java.lang.OutOfMemoryError",)),
        ("vineflower", "A.java", VF_CLASS, 1, ("java.lang.IllegalStateException",)),
        ("vineflower", "A.java", VF_CLASS_EMPTY, 1, ()),
        ("vineflower", "A.java", VF_INLINE, 1, ()),
        ("vineflower", "A.java", VF_LAMBDA, 1, ()),
        ("fernflower", "A.java", FF_METHOD, 1, ()),
        ("fernflower", "A.java", FF_LIMIT, 1, ()),
        ("cfr", "A.java", CFR_METHOD, 1, ("org.benf.cfr.reader.util.ConfusedCFRException",)),
        ("procyon", "A.java", PROCYON_METHOD, 1, ("java.lang.IllegalStateException",)),
        ("jd", "A.java", JD_METHOD, 1, ()),
    ],
)
def test_scan_finds_each_marker(tmp_path, engine, name, text, methods, errors):
    write(tmp_path, f"p/{name}", text)
    assert stubs.scan(tmp_path, engine) == [Stub(f"p/{name}", methods, errors)]


@pytest.mark.parametrize(
    "engine, text, errors",
    [
        ("vineflower", VF_METHOD, ("java.lang.NullPointerException",)),
        ("cfr", CFR_METHOD, ("org.benf.cfr.reader.util.ConfusedCFRException",)),
        ("procyon", PROCYON_METHOD, ("java.lang.IllegalStateException",)),
        ("jd", JD_METHOD, ()),
    ],
)
def test_scan_reads_windows_line_ends(tmp_path, engine, text, errors):
    (tmp_path / "A.java").write_bytes(text.replace("\n", "\r\n").encode())
    assert stubs.scan(tmp_path, engine) == [Stub("A.java", 1, errors)]


@pytest.mark.parametrize(
    "engine, text",
    [
        ("fernflower", '      buffer.append("// $FF: Couldn\'t be decompiled");\n'),
        ("vineflower", '      buffer.append("// $VF: Couldn\'t be decompiled");\n'),
        ("vineflower", '      buffer.append(" // $VF: Couldn\'t be decompiled");\n'),
        ("vineflower", '      buffer.append("/* $VF: Couldn\'t be decompiled. Class " + n + " wasn\'t processed yet! */");\n'),
        ("vineflower", '      lines.add("$VF: Unable to decompile class");\n'),
        ("vineflower", f"      // $VF: Could not inline inconsistent finally blocks\n      // {VF_PLEASE}\n      try {{\n"),
        ("fernflower", "   // $FF: renamed from: a\n   public int field1;\n"),
        ("fernflower", "   // $FF: synthetic class\n   static class A$1 {\n"),
        ("fernflower", "   // $FF: synthetic field\n   static boolean $assertionsDisabled;\n"),
        ("fernflower", "   // $FF: synthetic method\n   private static E[] $values() {\n"),
        ("fernflower", "   // $FF: bridge method\n   public Object get() {\n"),
        ("jd", "      // Byte code: goto -> 12\n"),
        ("vineflower", CFR_METHOD),
        ("cfr", VF_METHOD),
    ],
)
def test_scan_ignores_text_that_is_no_stub(tmp_path, engine, text):
    write(tmp_path, "p/A.java", text)
    assert stubs.scan(tmp_path, engine) == []


def test_scan_reads_only_source_files_and_sorts_by_path(tmp_path):
    write(tmp_path, "z/Z.java", VF_METHOD)
    write(tmp_path, "a/A.kt", VF_METHOD)
    write(tmp_path, "a/notes.txt", VF_METHOD)
    assert [s.path for s in stubs.scan(tmp_path, "vineflower")] == ["a/A.kt", "z/Z.java"]


def test_scan_unknown_engine_finds_nothing(tmp_path):
    write(tmp_path, "A.java", VF_METHOD)
    assert stubs.scan(tmp_path, "custom") == []
