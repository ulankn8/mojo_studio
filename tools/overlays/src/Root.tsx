import { Composition } from "remotion";
import { Bokeh, Leak, Lines, Sweep, Waves } from "./Overlays";

const FPS = 25;
const SIZE = { width: 1920, height: 1080, fps: FPS };

// ids are the file names the app expects in toystudio/assets/overlays/<id>.mp4
export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition id="leak" component={Leak} durationInFrames={4 * FPS} {...SIZE} />
      <Composition id="lines" component={Lines} durationInFrames={3 * FPS} {...SIZE} />
      <Composition id="waves" component={Waves} durationInFrames={5 * FPS} {...SIZE} />
      <Composition id="bokeh" component={Bokeh} durationInFrames={4 * FPS} {...SIZE} />
      <Composition id="sweep" component={Sweep} durationInFrames={Math.round(1.6 * FPS)} {...SIZE} />
    </>
  );
};
