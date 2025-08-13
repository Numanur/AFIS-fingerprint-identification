using System.Text.Json;
using System.Text.Json.Serialization;
using SourceAFIS;

record IdentifyResult(string? match_id, double score, double threshold);
record CalibrateResult(double suggested_threshold, double target_far, int impostor_pairs);

static class AfisCli
{
    static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false
    };

static FingerprintTemplate TemplateFromImage(string path, int dpi)
{
    var bytes = File.ReadAllBytes(path);
    var opts  = new FingerprintImageOptions { Dpi = dpi }; // C# uses options with a Dpi property
    var img   = new FingerprintImage(bytes, opts);
    return new FingerprintTemplate(img);
}

    static void Enroll(string galleryRoot, string dbRoot, int dpi)
    {
        foreach (var personDir in Directory.EnumerateDirectories(galleryRoot))
        {
            var pid = new DirectoryInfo(personDir).Name;
            var outDir = Path.Combine(dbRoot, pid);
            Directory.CreateDirectory(outDir);

            var files = Directory
                .EnumerateFiles(personDir)
                .Where(f => f.EndsWith(".png", true, null) ||
                            f.EndsWith(".jpg", true, null) ||
                            f.EndsWith(".jpeg", true, null) ||
                            f.EndsWith(".bmp", true, null))
                .OrderBy(f => f, StringComparer.OrdinalIgnoreCase)
                .ToList();

            foreach (var f in files)
            {
                try
                {
                    var templ = TemplateFromImage(f, dpi);
                    var bytes = templ.ToByteArray();
                    var outPath = Path.Combine(outDir, Path.GetFileNameWithoutExtension(f) + ".fpt");
                    File.WriteAllBytes(outPath, bytes);
                }
                catch { /* skip unreadable */ }
            }
        }
    }

    static IdentifyResult Identify(string probePath, string dbRoot, double threshold, int dpi)
    {
        var probe = TemplateFromImage(probePath, dpi);
        var matcher = new FingerprintMatcher(probe);

        string? bestId = null;
        double bestScore = double.NegativeInfinity;

        foreach (var personDir in Directory.EnumerateDirectories(dbRoot))
        {
            var pid = new DirectoryInfo(personDir).Name;
            foreach (var tpath in Directory.EnumerateFiles(personDir, "*.fpt"))
            {
                try
                {
                    var cand = new FingerprintTemplate(File.ReadAllBytes(tpath));
                    var s = matcher.Match(cand);
                    if (s > bestScore) { bestScore = s; bestId = pid; }
                }
                catch { /* ignore */ }
            }
        }

        var matchId = (bestScore >= threshold) ? bestId : null;
        return new IdentifyResult(matchId, bestScore, threshold);
    }

    static CalibrateResult Calibrate(string galleryRoot, string dbRoot, double targetFar, int dpi)
    {
        Enroll(galleryRoot, dbRoot, dpi); // ensure templates exist

        var personToTemps = new Dictionary<string, List<FingerprintTemplate>>(StringComparer.OrdinalIgnoreCase);
        foreach (var personDir in Directory.EnumerateDirectories(dbRoot))
        {
            var pid = new DirectoryInfo(personDir).Name;
            var temps = Directory.EnumerateFiles(personDir, "*.fpt").Select(p =>
            {
                try { return new FingerprintTemplate(File.ReadAllBytes(p)); }
                catch { return null; }
            }).Where(t => t is not null).Cast<FingerprintTemplate>().ToList();

            if (temps.Count > 0) personToTemps[pid] = temps;
        }

        var impostor = new List<double>(1024);
        var pids = personToTemps.Keys.OrderBy(x => x, StringComparer.OrdinalIgnoreCase).ToList();
        for (int i = 0; i < pids.Count; i++)
            for (int j = i + 1; j < pids.Count; j++)
                foreach (var a in personToTemps[pids[i]])
                {
                    var m = new FingerprintMatcher(a);
                    foreach (var b in personToTemps[pids[j]])
                        impostor.Add(m.Match(b));
                }

        double suggested = 40.0; // reasonable default ~0.01% FMR
        if (impostor.Count > 0)
        {
            impostor.Sort(); // ascending
            var q = 1.0 - targetFar;
            var idx = Math.Clamp((int)Math.Floor(q * (impostor.Count - 1)), 0, impostor.Count - 1);
            suggested = impostor[idx];
        }

        return new CalibrateResult(suggested, targetFar, impostor.Count);
    }

    static int Main(string[] args)
    {
        // Commands:
        // enroll    --gallery <imagesRoot> --db <dbRoot> [--dpi 500]
        // identify  --probe <imagePath> --db <dbRoot> [--threshold 40] [--dpi 500]
        // calibrate --gallery <imagesRoot> --db <dbRoot> [--far 0.001] [--dpi 500]
        try
        {
            if (args.Length == 0) { PrintHelp(); return 2; }
            var cmd = args[0].ToLowerInvariant();
            var dict = ParseArgs(args.Skip(1));
            int dpi = GetInt(dict, "dpi", 500);

            switch (cmd)
            {
                case "enroll":
                    Enroll(Must(dict, "gallery"), Must(dict, "db"), dpi);
                    Console.WriteLine("{\"ok\":true}");
                    return 0;
                case "identify":
                {
                    var res = Identify(
                        Must(dict, "probe"),
                        Must(dict, "db"),
                        GetDouble(dict, "threshold", 40.0),
                        dpi);
                    Console.WriteLine(JsonSerializer.Serialize(res, JsonOpts));
                    return 0;
                }
                case "calibrate":
                {
                    var res = Calibrate(
                        Must(dict, "gallery"),
                        Must(dict, "db"),
                        GetDouble(dict, "far", 0.001),
                        dpi);
                    Console.WriteLine(JsonSerializer.Serialize(res, JsonOpts));
                    return 0;
                }
                default:
                    PrintHelp();
                    return 2;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.ToString());
            return 1;
        }
    }

    static void PrintHelp() => Console.WriteLine(
@"SourceAFIS CLI (C#)
enroll    --gallery <imagesRoot> --db <dbRoot> [--dpi 500]
identify  --probe <imagePath> --db <dbRoot> [--threshold 40] [--dpi 500]
calibrate --gallery <imagesRoot> --db <dbRoot> [--far 0.001] [--dpi 500]");

    static Dictionary<string,string> ParseArgs(IEnumerable<string> a)
    {
        var d = new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
        string? k = null;
        foreach (var tok in a)
        {
            if (tok.StartsWith("--")) { k = tok[2..]; d[k] = "true"; }
            else if (k is not null) { d[k] = tok; k = null; }
        }
        return d;
    }

    static string Must(Dictionary<string,string> d, string k)
        => d.TryGetValue(k, out var v) ? v : throw new ArgumentException($"Missing --{k}");
    static int GetInt(Dictionary<string,string> d, string k, int def)
        => d.TryGetValue(k, out var v) && int.TryParse(v, out var n) ? n : def;
    static double GetDouble(Dictionary<string,string> d, string k, double def)
        => d.TryGetValue(k, out var v) && double.TryParse(v, out var n) ? n : def;
}

