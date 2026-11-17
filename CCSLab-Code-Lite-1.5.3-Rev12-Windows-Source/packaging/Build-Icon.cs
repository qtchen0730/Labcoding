// Build a multi-size Windows icon from the repository PNG using GDI+.
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;

static class BuildIcon
{
    static int Main(string[] args)
    {
        if (args.Length != 3) throw new ArgumentException("PNG, ICO and PNG paths required.");
        var source = new Bitmap(args[0]);
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(args[1])));
        int[] sizes = {16,24,32,48,64,128,256};
        var frames = new byte[sizes.Length][];
        for (int i=0; i<sizes.Length; i++)
        using (var bitmap = new Bitmap(source, sizes[i], sizes[i]))
        using (var buffer = new MemoryStream())
        {
            bitmap.Save(buffer, ImageFormat.Png);
            frames[i] = buffer.ToArray();
            if (sizes[i] == 256) bitmap.Save(args[2], ImageFormat.Png);
        }
        using (var writer = new BinaryWriter(File.Create(args[1])))
        {
            writer.Write((ushort)0); writer.Write((ushort)1); writer.Write((ushort)sizes.Length);
            int offset = 6+16*sizes.Length;
            for (int i=0; i<sizes.Length; i++)
            {
                writer.Write((byte)(sizes[i] == 256 ? 0 : sizes[i]));
                writer.Write((byte)(sizes[i] == 256 ? 0 : sizes[i]));
                writer.Write((byte)0); writer.Write((byte)0);
                writer.Write((ushort)1); writer.Write((ushort)32);
                writer.Write(frames[i].Length); writer.Write(offset); offset += frames[i].Length;
            }
            foreach (var frame in frames) writer.Write(frame);
        }
        return 0;
    }
}
