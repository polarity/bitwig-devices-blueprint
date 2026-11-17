import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;

/**
 * Runs a pinned Bitwig Nitro front end, including semantic analysis,
 * on plain-text sources without launching the application.
 *
 * Usage: java -DinstallationRoot=<Bitwig dir> -cp <probe>:<bitwig.jar>:<libs.jar>
 *        [-Dnitro.llvm.output=<temporary directory>] [-Dnitro.sampleRate=44100]
 *        [-Dnitro.blockSize=1] [-Dnitro.frontend=windows-6.1.7] NitroSemanticProbe <source.nitro>...
 *
 * Every source is analysed on its own, one after another. Bitwig analyses the
 * cores of a device in parallel, and a cold parallel analysis can fail with
 * "Recursion in semantic analysis"; a serial run reports only source defects.
 * Output: one line per source, "OK<TAB>path" or "ERROR<TAB>path<TAB>message".
 * Host context such as the connected channel count is not modelled, so a
 * failing static_assert is context-dependent rather than a source defect.
 * The obfuscated entry points below are bound to the pinned JAR hash.
 */
public class NitroSemanticProbe {
    public static void main(String[] args) throws Exception {
        String frontend = System.getProperty("nitro.frontend", "windows-6.1.3");
        boolean current = switch (frontend) {
            case "windows-6.1.3" -> false;
            case "windows-6.1.7" -> true;
            default -> throw new IllegalArgumentException("Unknown Nitro semantic frontend: " + frontend);
        };
        Class<?> flags = Class.forName(current ? "com.bitwig.nitro.blr" : "com.bitwig.nitro.Xf");
        Object noFlags = flags.getConstructors()[0].newInstance(
            false, false, false, false, false, false, false, false, false, false, false, false);
        // Compiler settings: revision, chunk size, block size, sample rate,
        // oversampling factor, FFT order, ..., zero-init, absolute paths, flags.
        // A 1024-frame chunk turned audio ports into f32<1024> and rejected the
        // host-accepted Growl and Normalizer sources; 32 accepts them.
        int blockSize = Integer.getInteger("nitro.blockSize", 1);
        int sampleRate = Integer.getInteger("nitro.sampleRate", 44100);
        Object settings = Class.forName(current ? "com.bitwig.nitro.Hvv" : "com.bitwig.nitro.EWg").getConstructors()[0]
            .newInstance(0, 32, blockSize, sampleRate, 1.0, 0, 0, false, false, noFlags);

        // Plain-text code source of a Nitro atom. Its outer atom is only used
        // for identity, so an unconstructed instance is sufficient.
        Field unsafeField = Class.forName("sun.misc.Unsafe").getDeclaredField("theUnsafe");
        unsafeField.setAccessible(true);
        Object unsafe = unsafeField.get(null);
        Object atom = unsafe.getClass().getMethod("allocateInstance", Class.class)
            .invoke(unsafe, Class.forName(current ? "TLG" : "azQ"));
        Constructor<?> textSource = Class.forName(current ? "EwK" : "BYC").getDeclaredConstructors()[0];
        textSource.setAccessible(true);

        Class<?> codeSource = Class.forName(current ? "com.bitwig.nitro.vP2" : "com.bitwig.nitro.TJZ");
        Method compile = null;
        for (Method method : Class.forName(current ? "com.bitwig.nitro.tIb" : "com.bitwig.nitro.PI").getDeclaredMethods()) {
            Class<?>[] types = method.getParameterTypes();
            if (Modifier.isStatic(method.getModifiers()) && types.length == 8
                    && types[0] == String.class && types[1] == codeSource) {
                compile = method;
                break;
            }
        }
        if (compile == null) {
            throw new IllegalStateException("Nitro compile entry point not found in this JAR");
        }
        compile.setAccessible(true);

        String llvmOutput = System.getProperty("nitro.llvm.output");
        Field llvmText = null;
        if (llvmOutput != null) {
            // Compiler result field holding emitted LLVM text in each pinned build.
            llvmText = current ? Class.forName("com.bitwig.nitro.tM1").getDeclaredField("sCA")
                : Class.forName("com.bitwig.nitro.O2W").getDeclaredField("HEY");
            llvmText.setAccessible(true);
        }
        for (int index = 0; index < args.length; index++) {
            String path = Path.of(args[index]).toAbsolutePath().toString().replace('\\', '/');
            try {
                Object source = textSource.newInstance(atom, Files.readAllBytes(Path.of(path)), path);
                Object compiled = compile.invoke(null, path, source, settings,
                    new ArrayList<>(), new ArrayList<>(), new ArrayList<>(), new ArrayList<>(), path);
                if (llvmText != null) {
                    String ir = (String) llvmText.get(compiled);
                    if (ir == null || ir.isBlank()) {
                        throw new IllegalStateException("Nitro returned no LLVM IR for " + path);
                    }
                    Files.writeString(Path.of(llvmOutput, index + ".ll"), ir);
                }
                System.out.println("OK\t" + path);
            } catch (InvocationTargetException failure) {
                Throwable cause = failure.getCause();
                // The compiler wraps front-end errors; report the innermost message.
                while (cause.getCause() != null && cause.getCause() != cause) {
                    cause = cause.getCause();
                }
                String message = String.valueOf(cause.getMessage()).replace('\n', ' ').replace('\t', ' ');
                System.out.println("ERROR\t" + path + "\t" + cause.getClass().getSimpleName() + ": " + message);
            }
        }
    }
}
