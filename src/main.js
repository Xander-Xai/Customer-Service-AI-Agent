/**
 * 主聊天页入口
 * import 所有 CSS → Vite 自动合并
 */
import '../styles/variables.css';
import '../styles/layout.css';
import '../styles/components.css';
import '../styles/monitor.css';
import '../styles/animations.css';
import '../styles/responsive.css';

import { init } from './chat/index.js';

document.addEventListener('DOMContentLoaded', init);
