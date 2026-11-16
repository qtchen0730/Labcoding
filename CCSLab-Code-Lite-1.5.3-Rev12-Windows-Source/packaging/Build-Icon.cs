// Render the repository's vector monogram with Windows GDI+, without font dependencies.
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Text.RegularExpressions;
using System.Xml;

static class BuildIcon
{
    static GraphicsPath Parse(string text)
    {
        var tokens = Regex.Matches(text, @"[MLCZ]|-?\d+(?:\.\d+)?");
        var path = new GraphicsPath();
        var position = new PointF();
        int i = 0;
        Func<float> number = delegate { return float.Parse(tokens[i++].Value, CultureInfo.InvariantCulture); };
        Func<PointF> point = delegate { return new PointF(number(), number()); };
        while (i < tokens.Count)
        {
            string command = tokens[i++].Value;
            if (command == "M") { path.StartFigure(); position = point(); }
            else if (command == "L") { var next = point(); path.AddLine(position, next); position = next; }
            else if (command == "C")
            {
                var a = point(); var b = point(); var next = point();
                path.AddBezier(position, a, b, next); position = next;
            }
            else if (command == "Z") path.CloseFigure();
            else throw new InvalidDataException("Unsupported SVG command: " + command);
        }
        return path;
    }

    static Bitmap Render(XmlDocument svg, int size)
    {
        var result = new Bitmap(size, size, PixelFormat.Format32bppArgb);
        using (var large = new Bitmap(size*4, size*4, PixelFormat.Format32bppArgb))
        {
            using (var graphics = Graphics.FromImage(large))
            {
                graphics.Clear(Color.Transparent);
                graphics.SmoothingMode = SmoothingMode.AntiAlias;
                graphics.ScaleTransform(size*4f/256, size*4f/256);
                foreach (XmlNode node in svg.SelectNodes("//*[local-name()='path']"))
                using (var shape = Parse(node.Attributes["d"].Value))
                using (var brush = new SolidBrush(ColorTranslator.FromHtml(node.Attributes["fill"].Value)))
                    graphics.FillPath(brush, shape);
            }
            using (var graphics = Graphics.FromImage(result))
            {
                graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
                graphics.DrawImage(large, new Rectangle(0,0,size,size));
            }
        }
        return result;
    }

    static int Main(string[] args)
    {
        if (args.Length != 3) throw new ArgumentException("SVG, ICO and PNG paths required.");
        var svg = new XmlDocument { XmlResolver = null };
        svg.Load(args[0]);
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(args[1])));
        int[] sizes = {16,24,32,48,64,128,256};
        var frames = new byte[sizes.Length][];
        for (int i=0; i<sizes.Length; i++)
        using (var bitmap = Render(svg, sizes[i]))
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
