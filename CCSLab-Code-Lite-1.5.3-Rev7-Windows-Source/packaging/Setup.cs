using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("CCSLab Code Lite")]
[assembly: AssemblyProduct("CCSLab Code Lite")]
[assembly: AssemblyVersion("1.5.3.7")]
[assembly: AssemblyFileVersion("1.5.3.7")]

static class Setup
{
    const string Product = "CCSLab Code Lite";
    const string AppName = "CCSLab Code Lite.exe";
    const string Marker = ".ccslab-code-lite-install";
    const string RegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\CCSLabCodeLite";
    const string Version = "1.5.3 Rev7";
    static readonly string DefaultInstallDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", Product);
    static readonly string ShortcutPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.Programs), Product + ".lnk");

    [STAThread]
    static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try
        {
#if UNINSTALLER
            if (args.Length == 3 && args[0] == "--finish-uninstall")
            {
                FinishUninstall(Int32.Parse(args[1]), args[2]);
                return 0;
            }
            if (MessageBox.Show("卸载 CCSLab Code Lite？连接配置和历史会话将保留。",
                Product, MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK)
                return 0;
            var installedAt = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            ValidateInstallation(installedAt);
            var cleanup = Path.Combine(Path.GetTempPath(), "CCSLabCodeLite-uninstall-" + Guid.NewGuid().ToString("N") + ".exe");
            File.Copy(Assembly.GetExecutingAssembly().Location, cleanup, false);
            Start(cleanup, "--finish-uninstall " + Process.GetCurrentProcess().Id + " \"" + installedAt + "\"", Path.GetTempPath());
#else
            if (!Environment.Is64BitOperatingSystem)
                throw new InvalidOperationException("此安装包需要 Windows 10 或 Windows 11 x64。");
            if (args.Length == 2 && args[0] == "--smoke-install")
            {
                var testDirectory = NormalizeDestination(args[1]);
                RequireAppClosed(testDirectory);
                Install(testDirectory);
                return 0;
            }
            using (var wizard = new SetupForm())
                Application.Run(wizard);
#endif
            return 0;
        }
        catch (Exception error)
        {
            if (args.Length == 2 && args[0] == "--smoke-install")
            {
                try { File.WriteAllText(args[1] + ".error.txt", error.ToString()); } catch { }
            }
            else
                MessageBox.Show(error.Message, Product, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    static void ValidateInstallation(string folder)
    {
        if (!Directory.Exists(folder) ||
            (File.GetAttributes(folder) & FileAttributes.ReparsePoint) != 0 ||
            !File.Exists(Path.Combine(folder, Marker)))
            throw new IOException("找不到 CCSLab Code Lite 安装记录，请检查程序目录。");
    }

    static string RegisteredDirectory()
    {
        using (var key = Registry.CurrentUser.OpenSubKey(RegistryPath))
            return key == null ? null : key.GetValue("InstallLocation") as string;
    }

    static bool SamePath(string first, string second)
    {
        return String.Equals(first, second, StringComparison.OrdinalIgnoreCase);
    }

    static string NormalizeDestination(string input)
    {
        var text = Environment.ExpandEnvironmentVariables(input.Trim().Trim('"'));
        if (text.Length < 4 || !Char.IsLetter(text[0]) || text[1] != ':' ||
            (text[2] != '\\' && text[2] != '/'))
            throw new IOException("请输入完整的本地安装路径，例如 D:\\Apps\\CCSLab Code Lite。");
        var folder = Path.GetFullPath(text).TrimEnd(Path.DirectorySeparatorChar);
        if (folder.Length <= Path.GetPathRoot(folder).TrimEnd(Path.DirectorySeparatorChar).Length)
            throw new IOException("请选择磁盘中的子文件夹作为安装位置。");
        var data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "CCSLabCodeLite");
        if (SamePath(folder, data) || folder.StartsWith(data + "\\", StringComparison.OrdinalIgnoreCase))
            throw new IOException("请选择应用数据目录之外的位置。");
        for (var parent = new DirectoryInfo(folder); parent != null; parent = parent.Parent)
        {
            if (File.Exists(parent.FullName))
                throw new IOException("安装路径中存在同名文件，请选择其他位置。");
            if (parent.Exists && (parent.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new IOException("安装路径中存在目录链接，请直接选择目标文件夹。");
        }
        return folder;
    }

    static void RequireAppClosed(string directory)
    {
        foreach (Process process in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(AppName)))
        {
            using (process)
            {
                string executable;
                try { executable = process.MainModule.FileName; }
                catch (System.ComponentModel.Win32Exception) { continue; }
                if (SamePath(executable, Path.Combine(directory, AppName)))
                    throw new IOException("请先退出 CCSLab Code Lite，然后重试。");
            }
        }
    }

    static void Start(string path, string args, string workingFolder)
    {
        using (Process.Start(new ProcessStartInfo(path, args)
        {
            WorkingDirectory = workingFolder,
            UseShellExecute = false
        })) { }
    }

#if UNINSTALLER
    static void FinishUninstall(int firstProcess, string directory)
    {
        directory = NormalizeDestination(directory);
        try
        {
            using (var parent = Process.GetProcessById(firstProcess))
            {
                if (!parent.WaitForExit(15000))
                    throw new IOException("请关闭卸载窗口后重试。");
            }
        }
        catch (ArgumentException) { }
        RequireAppClosed(directory);
        ValidateInstallation(directory);
        RemoveInstalledFiles(directory, directory);
        if (SamePath(RegisteredDirectory(), directory))
        {
            File.Delete(ShortcutPath);
            Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, false);
        }
        MessageBox.Show("已卸载 CCSLab Code Lite。", Product, MessageBoxButtons.OK, MessageBoxIcon.Information);
    }
#else
    static void Install(string destination)
    {
        var root = Path.GetDirectoryName(destination);
        Directory.CreateDirectory(root);
        var stage = Path.Combine(root, ".ccslab-setup-" + Guid.NewGuid().ToString("N"));
        var previous = stage + ".old";
        bool hasInstallation = File.Exists(Path.Combine(destination, Marker));
        if (hasInstallation)
        {
            ValidateInstallation(destination);
            RequireOnlyInstalledFiles(destination);
        }
        else if (Directory.Exists(destination) && Directory.GetFileSystemEntries(destination).Length > 0)
            throw new IOException("目标文件夹已有其他文件。请选择空文件夹，或输入新的子文件夹路径。");
        Directory.CreateDirectory(stage);
        try
        {
            using (var package = Assembly.GetExecutingAssembly().GetManifestResourceStream("ccslab-payload"))
            using (var archive = new ZipArchive(package, ZipArchiveMode.Read))
            {
                var manifest = Path.Combine(stage, Marker);
                using (var files = new StreamWriter(manifest))
                {
                    foreach (var entry in archive.Entries)
                    {
                        var result = ResolvePayloadPath(stage, entry.FullName);
                        Directory.CreateDirectory(Path.GetDirectoryName(result));
                        using (var input = entry.Open())
                        using (var output = new FileStream(result, FileMode.CreateNew))
                            input.CopyTo(output);
                        files.WriteLine(entry.FullName);
                    }
                }
            }
            bool upgrading = Directory.Exists(destination);
            if (upgrading)
            {
                if (hasInstallation)
                    Directory.Move(destination, previous);
                else
                    Directory.Delete(destination, false);
            }
            try { Directory.Move(stage, destination); }
            catch
            {
                if (hasInstallation)
                    Directory.Move(previous, destination);
                throw;
            }
            if (hasInstallation)
                RemoveInstalledFiles(previous, previous);
        }
        finally
        {
            if (Directory.Exists(stage))
                Directory.Delete(stage, true);
        }
    }

    static void InstallForUser(string directory)
    {
        RequireAppClosed(directory);
        var registered = RegisteredDirectory();
        if (!String.IsNullOrWhiteSpace(registered) && !SamePath(directory, registered) &&
            File.Exists(Path.Combine(registered, Marker)))
            throw new IOException("如需更换安装位置，请先在 Windows「已安装的应用」中卸载旧版本，再选择新路径。历史和连接配置会保留。");
        Install(directory);
        var executable = Path.Combine(directory, AppName);
        using (var registry = Registry.CurrentUser.CreateSubKey(RegistryPath))
        {
            registry.SetValue("DisplayName", Product);
            registry.SetValue("DisplayVersion", Version);
            registry.SetValue("DisplayIcon", executable);
            registry.SetValue("InstallLocation", directory);
            registry.SetValue("UninstallString", "\"" + Path.Combine(directory, "Uninstall.exe") + "\"");
            registry.SetValue("NoModify", 1, RegistryValueKind.DWord);
            registry.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        }
        var shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
        var shortcut = shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod,
            null, shell, new object[] { ShortcutPath });
        try
        {
            shortcut.GetType().InvokeMember("TargetPath", BindingFlags.SetProperty,
                null, shortcut, new object[] { executable });
            shortcut.GetType().InvokeMember("WorkingDirectory", BindingFlags.SetProperty,
                null, shortcut, new object[] { directory });
            shortcut.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
        }
        finally
        {
            System.Runtime.InteropServices.Marshal.ReleaseComObject(shortcut);
            System.Runtime.InteropServices.Marshal.ReleaseComObject(shell);
        }
    }

    sealed class SetupForm : Form
    {
        readonly Button action;
        readonly Button close;
        readonly Button browse;
        readonly TextBox destination;
        readonly Label body;
        string installedDirectory;
        bool installed;
        bool working;

        public SetupForm()
        {
            Text = Product + " 安装";
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            AutoScaleMode = AutoScaleMode.Dpi;
            ClientSize = new Size(590, 267);
            var title = new Label {
                Text = Product, AutoSize = true, Location = new Point(30, 28),
                Font = new Font("Segoe UI", 21, FontStyle.Bold)
            };
            body = new Label {
                Text = "选择安装位置。可以使用默认位置、空文件夹或一个新路径。",
                Location = new Point(32, 84), Size = new Size(520, 37)
            };
            destination = new TextBox {
                Text = RegisteredDirectory() ?? DefaultInstallDir, AccessibleName = "安装路径",
                Location = new Point(32, 126), Size = new Size(407, 28)
            };
            browse = new Button { Text = "浏览…", Location = new Point(451, 122), Size = new Size(101, 32) };
            action = new Button { Text = "安装", Location = new Point(354, 210), Size = new Size(93, 32) };
            close = new Button { Text = "关闭", Location = new Point(459, 210), Size = new Size(93, 32) };
            browse.Click += Browse;
            action.Click += InstallOrLaunch;
            close.Click += delegate { Close(); };
            Controls.AddRange(new Control[] { title, body, destination, browse, action, close });
            AcceptButton = action;
            CancelButton = close;
        }

        void Browse(object sender, EventArgs e)
        {
            using (var picker = new FolderBrowserDialog {
                Description = "选择 CCSLab Code Lite 的安装文件夹",
                ShowNewFolderButton = true
            })
            {
                if (Directory.Exists(destination.Text))
                    picker.SelectedPath = destination.Text;
                if (picker.ShowDialog(this) == DialogResult.OK)
                    destination.Text = picker.SelectedPath;
            }
        }

        void InstallOrLaunch(object sender, EventArgs e)
        {
            if (installed)
            {
                try { Start(Path.Combine(installedDirectory, AppName), "", installedDirectory); Close(); }
                catch (Exception error) { ShowError(error); }
                return;
            }
            string selected;
            try { selected = NormalizeDestination(destination.Text); }
            catch (Exception error) { ShowError(error); return; }
            working = true;
            action.Enabled = close.Enabled = browse.Enabled = destination.Enabled = false;
            body.Text = "正在安装，请稍候…";
            var install = new System.ComponentModel.BackgroundWorker();
            install.DoWork += delegate { InstallForUser(selected); };
            install.RunWorkerCompleted += delegate(object finished, System.ComponentModel.RunWorkerCompletedEventArgs result)
            {
                working = false;
                action.Enabled = true;
                close.Enabled = true;
                installed = result.Error == null;
                if (installed)
                {
                    installedDirectory = selected;
                    destination.Text = selected;
                }
                else
                    browse.Enabled = destination.Enabled = true;
                action.Text = installed ? "启动" : "重试";
                body.Text = installed
                    ? "安装完成。可从 Windows 开始菜单启动 CCSLab Code Lite。"
                    : "安装遇到问题。修正后可重试。";
                if (!installed) ShowError(result.Error);
                install.Dispose();
            };
            install.RunWorkerAsync();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (working) e.Cancel = true;
            base.OnFormClosing(e);
        }

        void ShowError(Exception error)
        {
            MessageBox.Show(this, error.Message, Product, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
#endif

    static string ResolvePayloadPath(string folder, string file)
    {
        var root = Path.GetFullPath(folder).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var candidate = Path.GetFullPath(Path.Combine(root, file));
        if (!candidate.StartsWith(root, StringComparison.OrdinalIgnoreCase) ||
            file.IndexOf(':') >= 0 || candidate == root)
            throw new InvalidDataException("安装清单含有无效路径。");
        for (var directory = Path.GetDirectoryName(candidate); directory.Length >= root.Length;
            directory = Path.GetDirectoryName(directory))
        {
            if (Directory.Exists(directory) && (File.GetAttributes(directory) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("安装目录中存在重解析点。");
        }
        return candidate;
    }

    static void RequireOnlyInstalledFiles(string folder)
    {
        var manifest = Path.Combine(folder, Marker);
        var expected = new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase) { manifest };
        foreach (var line in File.ReadAllLines(manifest))
        {
            var file = ResolvePayloadPath(folder, line);
            for (var path = file; path.Length > folder.Length; path = Path.GetDirectoryName(path))
                expected.Add(path);
        }
        var directories = new System.Collections.Generic.Stack<string>();
        directories.Push(folder);
        while (directories.Count > 0)
        {
            foreach (var entry in Directory.GetFileSystemEntries(directories.Pop()))
            {
                var attributes = File.GetAttributes(entry);
                if (!expected.Contains(entry) || (attributes & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("旧安装目录含有额外文件或链接。请先移出自建内容，再原地升级。");
                if ((attributes & FileAttributes.Directory) != 0)
                    directories.Push(entry);
            }
        }
    }

    static void RemoveInstalledFiles(string folder, string manifestRoot)
    {
        var marker = Path.Combine(manifestRoot, Marker);
        var directories = new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in File.ReadAllLines(marker))
        {
            var file = ResolvePayloadPath(folder, line);
            File.Delete(file);
            for (var directory = Path.GetDirectoryName(file); directory.Length > folder.Length;
                directory = Path.GetDirectoryName(directory))
                directories.Add(directory);
        }
        var ordered = new System.Collections.Generic.List<string>(directories);
        ordered.Sort((left, right) => right.Length.CompareTo(left.Length));
        foreach (var directory in ordered)
            if (Directory.Exists(directory) && Directory.GetFileSystemEntries(directory).Length == 0)
                Directory.Delete(directory);
        File.Delete(marker);
        if (Directory.GetFileSystemEntries(folder).Length == 0)
            Directory.Delete(folder);
    }
}
