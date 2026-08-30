interface AppLogoProps {
  size?: number;
}

export default function AppLogo({ size = 34 }: AppLogoProps) {
  return (
    <span
      className="inline-flex items-center justify-center rounded-[3px] bg-pine shrink-0"
      style={{
        width: size,
        height: size,
        boxShadow: 'inset 0 0 0 1.5px rgba(243, 244, 240, 0.55)',
      }}
    >
      <span
        className="text-paper font-bold"
        style={{ fontSize: size * 0.5, lineHeight: 1 }}
      >
        知
      </span>
    </span>
  );
}
